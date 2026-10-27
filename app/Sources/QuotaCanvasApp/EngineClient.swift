import Foundation
import Darwin

/// Python エンジン（quota-canvas CLI）を Process で呼ぶクライアント。
/// アプリは AI サービスや USB 画面と直接通信しない。すべてこの経路 + 2 つのファイル。
final class EngineClient: @unchecked Sendable {
    private let decoder = JSONDecoder()

    // MARK: - engine path

    var enginePathRaw: String {
        UserDefaults.standard.string(forKey: AppDefaults.enginePathKey) ?? AppDefaults.enginePath
    }

    var enginePath: String {
        (enginePathRaw as NSString).expandingTildeInPath
    }

    private func trustedEnginePath(_ path: String) -> String? {
        let resolved = URL(fileURLWithPath: path).resolvingSymlinksInPath()
        let owner = getuid()
        var url = resolved
        var isExecutable = true
        while true {
            var info = stat()
            guard url.path.withCString({ fstatat(AT_FDCWD, $0, &info, 0) }) == 0,
                  info.st_uid == owner || info.st_uid == 0 else {
                return nil
            }
            let otherWritable = (info.st_mode & mode_t(S_IWOTH)) != 0
            let stickyRootDirectory = !isExecutable && info.st_uid == 0
                && (info.st_mode & mode_t(S_ISVTX)) != 0
            if otherWritable && !stickyRootDirectory {
                return nil
            }
            if url.path == "/" {
                break
            }
            url = url.deletingLastPathComponent()
            isExecutable = false
        }
        return resolved.path
    }

    // MARK: - process runner

    /// CLI を実行し stdout を返す。失敗時は EngineError（stdout の {"error": ...} を優先）。
    func run(_ args: [String], stdin: Data? = nil) async throws -> Data {
        let exe = enginePath
        let displayPath = enginePathRaw
        guard FileManager.default.isExecutableFile(atPath: exe) else {
            throw EngineError(message: String(localized: "エンジンが見つかりません: \(displayPath)\n設定タブでパスを確認してください。"))
        }
        guard let trustedExe = trustedEnginePath(exe) else {
            throw EngineError(message: String(localized: "エンジンの実行元が安全ではありません: \(displayPath)\n実行ファイルとパス上のフォルダの所有者・書込権限を確認してください。"))
        }

        return try await withCheckedThrowingContinuation { continuation in
            DispatchQueue.global(qos: .userInitiated).async {
                let process = Process()
                process.executableURL = URL(fileURLWithPath: trustedExe)
                process.arguments = args

                let stdout = Pipe()
                let stderr = Pipe()
                process.standardOutput = stdout
                process.standardError = stderr
                process.standardInput = FileHandle.nullDevice

                var stdinWriteError: Error?
                if let stdin {
                    let stdinPipe = Pipe()
                    process.standardInput = stdinPipe
                    do {
                        try stdinPipe.fileHandleForWriting.write(contentsOf: stdin)
                        try? stdinPipe.fileHandleForWriting.close()
                    } catch {
                        stdinWriteError = error
                    }
                }

                do {
                    try process.run()
                } catch {
                    continuation.resume(throwing: EngineError(message: String(localized: "エンジンを起動できません: \(error.localizedDescription)")))
                    return
                }

                let outData = stdout.fileHandleForReading.readDataToEndOfFile()
                let errData = stderr.fileHandleForReading.readDataToEndOfFile()
                process.waitUntilExit()

                if let stdinWriteError {
                    continuation.resume(throwing: stdinWriteError)
                    return
                }

                if process.terminationStatus != 0 {
                    continuation.resume(throwing: Self.engineError(stdout: outData, stderr: errData, status: process.terminationStatus))
                    return
                }
                if let envelope = try? JSONDecoder().decode(ErrorEnvelope.self, from: outData) {
                    // 成功でも {"error": ...} が返ったらエラー扱い
                    continuation.resume(throwing: EngineError(message: envelope.error))
                    return
                }
                continuation.resume(returning: outData)
            }
        }
    }

    private static func engineError(stdout: Data, stderr: Data, status: Int32) -> EngineError {
        if let envelope = try? JSONDecoder().decode(ErrorEnvelope.self, from: stdout), !envelope.error.isEmpty {
            return EngineError(message: envelope.error)
        }
        let errText = String(data: stderr, encoding: .utf8)?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        if !errText.isEmpty {
            let firstLines = errText.split(separator: "\n").suffix(3).joined(separator: "\n")
            return EngineError(message: String(localized: "エンジンエラー (exit \(Int(status))):\n\(firstLines)"))
        }
        return EngineError(message: String(localized: "エンジンコマンドが失敗しました (exit \(Int(status)))"))
    }

    private func decode<T: Decodable>(_ type: T.Type, from data: Data) throws -> T {
        do {
            return try decoder.decode(T.self, from: data)
        } catch {
            let preview = String(data: data.prefix(300), encoding: .utf8) ?? "(non-UTF8)"
            throw EngineError(message: String(localized: "エンジンの応答を解釈できません: \(error.localizedDescription)\n\(preview)"))
        }
    }

    // MARK: - typed commands

    /// `quota-canvas detect --json`（各サービスで利用状況を 1 回取得するため遅い）
    func detect() async throws -> [DetectService] {
        try decode([DetectService].self, from: try await run(["detect", "--json"]))
    }

    /// `quota-canvas config --json`
    func getConfig() async throws -> ConfigDocument {
        try ConfigDocument(data: try await run(["config", "--json"]))
    }

    /// `quota-canvas config --set-json`（stdin で全体を送り、反映後の設定を受け取る）
    func setConfig(_ doc: ConfigDocument) async throws -> ConfigDocument {
        let data = try doc.jsonData()
        let out = try await run(["config", "--set-json"], stdin: data)
        return try ConfigDocument(data: out)
    }

    /// `quota-canvas set-key <provider>`（キーは stdin で渡す。引数に載せない）
    func setKey(provider: String, key: String) async throws {
        _ = try await run(["set-key", provider], stdin: Data(key.trimmingCharacters(in: .whitespacesAndNewlines).utf8))
    }

    /// `quota-canvas set-key <provider> --delete`
    func deleteKey(provider: String) async throws {
        _ = try await run(["set-key", provider, "--delete"])
    }

    /// `quota-canvas refresh --json`（常駐ループに即時の再取得・再描画を依頼する）
    /// 失敗（プロセス異常終了・{"error": ...}・応答を解釈できない）ときはエラーを投げる。
    func refresh() async throws {
        struct RefreshReply: Decodable {
            var requested: Bool?
        }
        let reply = try decode(RefreshReply.self, from: try await run(["refresh", "--json"]))
        if reply.requested == false {
            throw EngineError(message: String(localized: "エンジンが反映要求を受け付けませんでした。"))
        }
    }

    /// 設定保存（config --set-json）後、エンジンが 1〜2 秒で描き直すのに合わせて
    /// 2 秒後に status.json / frame.png の再読み込みを促す通知を飛ばす。
    static func scheduleArtifactReload() {
        Task {
            try? await Task.sleep(nanoseconds: 2_000_000_000)
            NotificationCenter.default.post(name: .engineArtifactsChanged, object: nil)
        }
    }
}

private extension JSONDecoder {}
