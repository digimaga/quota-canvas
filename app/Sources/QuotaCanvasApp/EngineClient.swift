import Foundation
import Darwin

private final class PipeResults: @unchecked Sendable {
    var out = Data()
    var err = Data()
    var writeError: Error?
}

/// アプリ内の全設定保存を、読み込みから書き込みまで直列化する。
actor ConfigSaveLock {
    private var locked = false
    private var waiters: [CheckedContinuation<Void, Never>] = []
    func acquire() async {
        if !locked { locked = true; return }
        await withCheckedContinuation { waiters.append($0) }
    }
    func release() {
        if waiters.isEmpty { locked = false } else { waiters.removeFirst().resume() }
    }
}

/// Python エンジン（quota-canvas CLI）を Process で呼ぶクライアント。
/// アプリは AI サービスや USB 画面と直接通信しない。
final class EngineClient: @unchecked Sendable {
    private static let configLock = ConfigSaveLock()
    private let decoder = JSONDecoder()
    private let enginePathOverride: String?

    init(enginePathOverride: String? = nil) { self.enginePathOverride = enginePathOverride }

    // MARK: - engine path

    var enginePathRaw: String {
        enginePathOverride ?? UserDefaults.standard.string(forKey: AppDefaults.enginePathKey).flatMap { $0.isEmpty ? nil : $0 } ?? AppDefaults.enginePath
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

                let input = stdin.map { _ in Pipe() }
                if let input { process.standardInput = input }
                do {
                    try process.run()
                } catch {
                    continuation.resume(throwing: EngineError(message: String(localized: "エンジンを起動できません: \(error.localizedDescription)")))
                    return
                }

                // どの pipe も同時に処理し、子プロセスの出力待ちとの循環を避ける。
                let group = DispatchGroup()
                let output = PipeResults()
                group.enter()
                DispatchQueue.global().async {
                    output.out = stdout.fileHandleForReading.readDataToEndOfFile()
                    group.leave()
                }
                group.enter()
                DispatchQueue.global().async {
                    output.err = stderr.fileHandleForReading.readDataToEndOfFile()
                    group.leave()
                }
                if let stdin, let input {
                    group.enter()
                    DispatchQueue.global().async {
                        do { try input.fileHandleForWriting.write(contentsOf: stdin) }
                        catch { output.writeError = error }
                        try? input.fileHandleForWriting.close()
                        group.leave()
                    }
                }
                process.waitUntilExit()
                group.wait()
                let outData = output.out
                let errData = output.err
                if let error = output.writeError {
                    continuation.resume(throwing: error)
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

    func updateConfig(_ mutate: @escaping (inout ConfigDocument) -> Void) async throws -> ConfigDocument {
        await Self.configLock.acquire()
        do {
            var latest = try await getConfig()
            mutate(&latest)
            let saved = try await setConfig(latest)
            await Self.configLock.release()
            return saved
        } catch {
            await Self.configLock.release()
            throw error
        }
    }

    func saveChanges(from original: ConfigDocument, to edited: ConfigDocument) async throws -> ConfigDocument {
        try await updateConfig { latest in
            latest = ConfigDocument.applyingChanges(from: original, to: edited, onto: latest)
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

    // MARK: - routing modes

    func routingModes() async throws -> [RoutingModeEntry] {
        struct Reply: Decodable { var modes: [RoutingModeEntry] }
        return try decode(Reply.self, from: try await run(["routing", "mode", "--json"])).modes
    }

    @discardableResult
    func setRoutingMode(name: String, mode: String) async throws -> [RoutingModeEntry] {
        struct Reply: Decodable { var modes: [RoutingModeEntry] }
        let out = try await run(["routing", "mode", name, mode, "--json"])
        return try decode(Reply.self, from: out).modes
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
