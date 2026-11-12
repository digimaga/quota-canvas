import Foundation
import Darwin

/// 1 回の子プロセス実行の共有状態。期限タイマー・waitpid・pipe 読み取りが
/// 別スレッドで往復するので NSLock で守る。
private final class ChildRun: @unchecked Sendable {
    let lock = NSLock()
    var out = Data()
    var err = Data()
    var writeError: Error?
    var exited = false           // waitpid が戻った
    var reaping = false          // 子の終了を見た（ここから先はシグナルを送らない。回収はこの後）
    var exitStatus: Int32 = -1   // WEXITSTATUS 相当（シグナル死はシグナル番号）
    var exitAt: TimeInterval = 0 // waitpid が戻った単調時刻
    var timedOut = false         // 期限でシグナルを送った

    func markExit(status: Int32, at time: TimeInterval) {
        lock.lock()
        exited = true
        exitStatus = status
        exitAt = time
        lock.unlock()
    }

    func markTimedOut() {
        lock.lock()
        timedOut = true
        lock.unlock()
    }

    var didTimeOut: Bool {
        lock.lock()
        defer { lock.unlock() }
        return timedOut
    }
}

/// pipe 読み取りループの poll 刻み。子の終了で読み取り期限が縮んでも、
/// 眠っている poll をこの間隔で起こして期限を再判定する。
private let enginePipePollTick: TimeInterval = 0.5

/// 単調時計（秒）。期限の計算に使う（壁面時計は NVM 等で動くので使わない）。
private func engineMonotonicNow() -> TimeInterval {
    Double(DispatchTime.now().uptimeNanoseconds) / 1_000_000_000
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

    /// 子プロセスの既定タイムアウト（秒）。読み取り系は短く、設定保存は長め、
    /// detect などネットワークを使うものはさらに長め。呼び出し側が timeout を
    /// 渡した場合はそちらを優先。テストで短くできるよう var にしてある
    /// （アプリからは変更しない）。
    static var readTimeout: TimeInterval = 10
    static var saveTimeout: TimeInterval = 30
    static var planningTimeout: TimeInterval = 20
    /// muse コマンドの既定。`muse-action-id --refresh` は HTML が参照する全 JS
    /// の取得・解決を RESOLVE_BUDGET=60 秒まで続けうる（src/quotacanvas/providers/
    /// muse.py の RESOLVE_BUDGET）。余裕を足して 90 秒。アプリの MuseUsagePoller は
    /// WebKit の期限を明示 timeout として渡すので、この既定は明示のない呼び出し
    /// （muse-ingest などローカル処理）の保険。
    static var museTimeout: TimeInterval = 90
    /// detect（`detect --json`）の既定。エンジンの detect_all は 12 サービスを
    /// 直列に 1 回ずつ取得する（src/quotacanvas/providers/__init__.py の detect_all）:
    /// HTTP は 1 回 15 秒（providers/_base.py TIMEOUT）で claude 1 + codex 2
    /// （利用量とリセット権）+ glm 1 + grok 1 + openrouter 2 + deepseek 1 +
    /// kimi 1 + opencode 1 の最大 10 回 = 150 秒、gemini は agy usage だけで
    /// 30 秒（providers/gemini.py USAGE_TIMEOUT）、muse は HTML+JS 取得で最大
    /// 約 20 秒、opendesign は (15+3+7.5)×2 = 51 秒（一時失敗の 1 回再試行込み）。
    /// 最悪合計は約 251 秒で、エンジン起動とパースの余裕を足して 300 秒。
    /// （`detect --cached` はキャッシュ欠損時にこの全取得に落ちるので同じ値）
    static var detectTimeout: TimeInterval = 300
    /// 期限で SIGTERM を送ったあと、無視されていた場合に SIGKILL を送るまでの猶予。
    static var killGrace: TimeInterval = 5
    /// 子が終わったあとも孫プロセスが pipe を握っていて EOF が来ないときに、
    /// 読み取りを打ち切るまでの猶予。これで continuation が必ず完了する。
    static var readGrace: TimeInterval = 5
    /// 子が終わったあと孫が stdin の読み口を握ったまま読まないときに、stdin への
    /// 書き込みを諦めて書き口を閉じるまでの猶予。期限の kill は子の生存中にしか
    /// 働かないので、これがないと blocking な書き込みが group.wait() を止める。
    static var stdinWriteGrace: TimeInterval = 5

    /// コマンドごとの既定タイムアウト。子プロセスが戻らないとき、configLock を
    /// 握ったままの設定保存や通知計画が永久に止まらないようにするため。
    static func defaultTimeout(for arguments: [String]) -> TimeInterval {
        switch arguments.first {
        case "detect":
            return detectTimeout
        case "config":
            return arguments.contains("--apply-json") || arguments.contains("--set-json") ? saveTimeout : readTimeout
        case "notifications-plan":
            return planningTimeout
        case "muse-action-id", "muse-ingest":
            return museTimeout
        default:
            return readTimeout
        }
    }

    /// CLI を実行し stdout を返す。失敗時は EngineError（stdout の {"error": ...} を優先）。
    /// timeout（無指定ならコマンドごとの既定）を過ぎたら子を新しいプロセスグループ
    /// ごと SIGTERM し、killGrace 経過後も終了しなければ SIGKILL する（子は
    /// POSIX_SPAWN_SETPGROUP でグループリーダーにしてあるので、stdout / stderr を
    /// 継承した孫プロセスも一緒に止まる）。子が終わったあとも孫などが pipe を握って
    /// EOF が来なければ readGrace で読み取りを打ち切るので、continuation はどんな
    /// 場合でも一度だけ完了する。
    func run(_ args: [String], stdin: Data? = nil, timeout: TimeInterval? = nil) async throws -> Data {
        let exe = enginePath
        let displayPath = enginePathRaw
        guard FileManager.default.isExecutableFile(atPath: exe) else {
            throw EngineError(message: String(localized: "エンジンが見つかりません: \(displayPath)\n設定タブでパスを確認してください。"))
        }
        guard let trustedExe = trustedEnginePath(exe) else {
            throw EngineError(message: String(localized: "エンジンの実行元が安全ではありません: \(displayPath)\n実行ファイルとパス上のフォルダの所有者・書込権限を確認してください。"))
        }
        let effectiveTimeout = timeout ?? Self.defaultTimeout(for: args)

        return try await withCheckedThrowingContinuation { continuation in
            DispatchQueue.global(qos: .userInitiated).async {
                Self.runChild(exe: trustedExe, args: args, stdin: stdin,
                              timeout: effectiveTimeout, continuation: continuation)
            }
        }
    }

    /// 子を新しいプロセスグループで起動し、pipe の読み書き・終了待ち・期限まで
    /// 面倒を見る。どの経路でも continuation を必ず一度だけ resume する。
    private static func runChild(exe: String, args: [String], stdin: Data?,
                                 timeout: TimeInterval, continuation: CheckedContinuation<Data, Error>) {
        // 親側が持つ pipe の fd の所有者は 1 つに決め、どの経路（通常終了・期限・
        // readGrace の打ち切り・エラー）でもちょうど 1 回だけ閉じる:
        // - inRead / outWrite / errWrite（子側の口。inRead は spawn の dup2 用）
        //   → spawn を呼んだ直後に runChild が閉じる
        // - inWrite → writeStdin が閉じる（defer で 1 回）
        // - outRead / errRead → readPipes が閉じる（EOF で open から外すとき、
        //   期限で打ち切るときのそれぞれ 1 回）
        // spawn に失敗したときだけ、後者を runChild が直接閉じる。
        func makePipe() -> (read: Int32, write: Int32)? {
            var fds: [Int32] = [-1, -1]
            guard pipe(&fds) == 0 else { return nil }
            return (fds[0], fds[1])
        }
        let inPipe = stdin != nil ? makePipe() : nil
        let outPipe = makePipe()
        let errPipe = makePipe()
        guard let outRead = outPipe?.read, let outWrite = outPipe?.write,
              let errRead = errPipe?.read, let errWrite = errPipe?.write else {
            // pipe が作れない（fd の枯渇など）。作れた分だけ閉じて continuation を
            // 完了させる（呼び出し側が設定ロックを握ったまま止まらないように）。
            for pipe in [inPipe, outPipe, errPipe] {
                guard let pipe else { continue }
                close(pipe.read)
                close(pipe.write)
            }
            continuation.resume(throwing: EngineError(message: String(localized:
                "エンジンとのパイプを開けませんでした: \(String(cString: strerror(errno)))")))
            return
        }
        let inRead = inPipe?.read ?? -1
        let inWrite = inPipe?.write ?? -1
        // 親側の stdin 書き口は nonblocking にする。孫が読み口を握ったまま読まないと
        // blocking write は永遠に戻らず、期限の kill も子の死亡後には働かないので
        // group.wait() が戻らなくなってしまうため。F_SETNOSIGPIPE で、子が stdin を
        // 読まずに終わったときの SIGPIPE（既定の動作ではこのプロセスごと終了する）を
        // write の EPIPE として受け取る。書き口への設定なので、子に渡す読み口（fd 0）
        // のブロック設定には影響しない。
        if inWrite >= 0 {
            let flags = fcntl(inWrite, F_GETFL)
            if flags >= 0 { _ = fcntl(inWrite, F_SETFL, flags | O_NONBLOCK) }
            _ = fcntl(inWrite, F_SETNOSIGPIPE, 1)
        }

        // 子を新しいプロセスグループのリーダーにする（SETPGROUP + pgroup 0）。
        // 期限での kill(-pid) が孫まで届くようにするため。CLOEXEC_DEFAULT で
        // ファイルアクションで渡す fd 以外は子に渡さない。
        var actions: posix_spawn_file_actions_t? = nil
        posix_spawn_file_actions_init(&actions)
        if stdin != nil {
            posix_spawn_file_actions_adddup2(&actions, inRead, 0)
            posix_spawn_file_actions_addclose(&actions, inRead)
            posix_spawn_file_actions_addclose(&actions, inWrite)
        } else {
            posix_spawn_file_actions_addopen(&actions, 0, "/dev/null", O_RDONLY, 0)
        }
        posix_spawn_file_actions_adddup2(&actions, outWrite, 1)
        posix_spawn_file_actions_addclose(&actions, outWrite)
        posix_spawn_file_actions_adddup2(&actions, errWrite, 2)
        posix_spawn_file_actions_addclose(&actions, errWrite)
        posix_spawn_file_actions_addclose(&actions, outRead)
        posix_spawn_file_actions_addclose(&actions, errRead)

        var attrs: posix_spawnattr_t? = nil
        posix_spawnattr_init(&attrs)
        posix_spawnattr_setflags(&attrs, Int16(POSIX_SPAWN_SETPGROUP | POSIX_SPAWN_CLOEXEC_DEFAULT))
        posix_spawnattr_setpgroup(&attrs, 0)

        var argv: [UnsafeMutablePointer<CChar>?] = [strdup(exe)] + args.map { strdup($0) }
        argv.append(nil)
        var pid: pid_t = -1
        let rc = posix_spawn(&pid, exe, &actions, &attrs, &argv, environ)
        for p in argv where p != nil { free(p) }
        posix_spawn_file_actions_destroy(&actions)
        posix_spawnattr_destroy(&attrs)
        // 親側の子側の口は直ちに閉じる（残すと自分で EOF を受け取れなくなる）。
        if stdin != nil { close(inRead) }
        close(outWrite)
        close(errWrite)

        guard rc == 0, pid > 0 else {
            if inWrite >= 0 { close(inWrite) }
            close(outRead)
            close(errRead)
            let reason = rc != 0 ? String(cString: strerror(rc)) : "posix_spawn failed"
            continuation.resume(throwing: EngineError(message: String(localized: "エンジンを起動できません: \(reason)")))
            return
        }

        let child = ChildRun()
        let startedAt = engineMonotonicNow()
        let group = DispatchGroup()

        // 期限: 子と孫をプロセスグループごと止める。子が先に死んでいたら
        // （waitpid で回収済み）pid の再利用を避けるため何もしない。
        DispatchQueue.global().asyncAfter(deadline: .now() + timeout) {
            // シグナルはロックを持ったまま送る。waitpid での回収は reaping を立てた
            // 後（同じロックの下）にしか起きないので、送る時点の pid は生きているか
            // 回収前のゾンビで、別のプロセスに再利用されていない。
            child.lock.lock()
            let alive = !child.reaping
            if alive {
                child.timedOut = true
                kill(-pid, SIGTERM)
                kill(pid, SIGTERM)
            }
            child.lock.unlock()
            guard alive else { return }
            DispatchQueue.global().asyncAfter(deadline: .now() + Self.killGrace) {
                child.lock.lock()
                if !child.reaping {
                    kill(-pid, SIGKILL)
                    kill(pid, SIGKILL)
                }
                child.lock.unlock()
            }
        }

        // waitpid は専用スレッドで。戻ったら終了ステータスと時刻を記録し、
        // タイマー（二度と kill しない）と読み取り（期限を終了時刻基準に縮める）に伝わる。
        group.enter()
        DispatchQueue.global().async {
            // まず回収せずに終了を待ち（WNOWAIT）、シグナルを送らない印を立ててから
            // 回収する。回収と期限のシグナル送信を同じロックで直列にするため。
            var info = siginfo_t()
            while waitid(P_PID, id_t(pid), &info, WEXITED | WNOWAIT) == -1 && errno == EINTR {}
            child.lock.lock()
            child.reaping = true
            child.lock.unlock()
            var status: Int32 = 0
            while waitpid(pid, &status, 0) == -1 && errno == EINTR {}
            // WIFEXITED / WEXITSTATUS / WTERMSIG は関数型マクロで Swift から
            // 見えないので wait ステータスを直接解読する。
            //   終了: 下位 7 ビット 0, 終了コードは上位 8 ビット
            //   シグナル死: 下位 7 ビット = シグナル番号（0x7F は停止）
            let low = status & 0x7F
            let code: Int32
            if low == 0 {
                code = (status >> 8) & 0xFF
            } else if low != 0x7F {
                code = low
            } else {
                code = status == 0 ? 0 : 1
            }
            child.markExit(status: code, at: engineMonotonicNow())
            group.leave()
        }

        // stdin は専用スレッドで書き切る。読み口を握ったまま読まない孫が残っても
        // group.wait() が必ず戻るよう、書き込みにも期限（子の生存中は timeout +
        // killGrace、子の終了後は終了時刻 + stdinWriteGrace）を設け、期限を過ぎたら
        // 書き込みを諦めて書き口を閉じる。子が先に死んで EPIPE になっても終了
        // ステータスで分かるのでエラーにしない。それ以外の書き込みエラーは伝える。
        if let stdin, inWrite >= 0 {
            group.enter()
            DispatchQueue.global().async {
                Self.writeStdin(stdin, to: inWrite, child: child,
                                startedAt: startedAt, timeout: timeout)
                group.leave()
            }
        }

        // stdout / stderr を読む。子が死んだあとも孫が書き口を握って EOF が来ない
        // ときは、終了時刻 + readGrace で読み口を閉じて打ち切る（continuation を
        // 必ず完了させるため）。子が生きている間の上限は timeout + killGrace + readGrace。
        group.enter()
        DispatchQueue.global().async {
            let data = Self.readPipes([outRead, errRead], child: child,
                                      startedAt: startedAt, timeout: timeout)
            child.lock.lock()
            child.out = data[outRead] ?? Data()
            child.err = data[errRead] ?? Data()
            child.lock.unlock()
            group.leave()
        }

        group.wait()
        child.lock.lock()
        let outData = child.out
        let errData = child.err
        let writeError = child.writeError
        let exitStatus = child.exitStatus
        child.lock.unlock()

        if let writeError {
            continuation.resume(throwing: writeError)
            return
        }
        if child.didTimeOut {
            continuation.resume(throwing: EngineError(message: String(localized:
                "エンジンが \(Int(timeout)) 秒で応答しないため打ち切りました: \(args.first ?? "コマンド")")))
            return
        }
        if exitStatus != 0 {
            continuation.resume(throwing: Self.engineError(stdout: outData, stderr: errData, status: exitStatus))
            return
        }
        if let envelope = try? JSONDecoder().decode(ErrorEnvelope.self, from: outData) {
            // 成功でも {"error": ...} が返ったらエラー扱い
            continuation.resume(throwing: EngineError(message: envelope.error))
            return
        }
        continuation.resume(returning: outData)
    }

    /// pipe を EOF か期限まで poll + read で読む。blocking read を使わないのは、
    /// 孫が書き口を握ったまま残って EOF が来ないときに自力で抜けるため。
    private static func readPipes(_ fds: [Int32], child: ChildRun,
                                  startedAt: TimeInterval, timeout: TimeInterval) -> [Int32: Data] {
        var buffers = Dictionary(uniqueKeysWithValues: fds.map { ($0, Data()) })
        var open = fds
        while !open.isEmpty {
            child.lock.lock()
            let exited = child.exited
            let exitAt = child.exitAt
            child.lock.unlock()
            let deadline = exited ? exitAt + readGrace
                                  : startedAt + timeout + killGrace + readGrace
            let remaining = deadline - engineMonotonicNow()
            if remaining <= 0 { break }
            var pollfds = open.map { pollfd(fd: $0, events: Int16(POLLIN), revents: 0) }
            let ms = Int32(min(remaining, enginePipePollTick) * 1000)
            let count = pollfds.withUnsafeMutableBufferPointer {
                poll($0.baseAddress, nfds_t($0.count), ms)
            }
            if count < 0 {
                if errno == EINTR { continue }
                break
            }
            if count == 0 { continue }
            var done: [Int32] = []
            for p in pollfds where p.revents != 0 {
                var chunk = [UInt8](repeating: 0, count: 65536)
                let n = read(p.fd, &chunk, chunk.count)
                if n > 0 {
                    buffers[p.fd]?.append(contentsOf: chunk[0..<n])
                } else if n == 0 || errno != EINTR {
                    done.append(p.fd)  // EOF か復旧できない口
                }
            }
            // EOF などを迎えた口は open から外すときにその場で閉じる。閉じるのは
            // このタイミングの 1 回だけなので、ループを抜けたあとの close と
            // 二重にはならない。
            for fd in done {
                close(fd)
                open.removeAll { $0 == fd }
            }
        }
        // 期限で打ち切ったときにまだ open な口を閉じる（通常実行では EOF で
        // 全て閉じ済みなので、ここは空になる）。
        for fd in open { close(fd) }
        return buffers
    }

    /// stdin への書き込み。nonblocking な書き口に poll(POLLOUT) で書けるように
    /// なるのを待ちながら書き、期限（子の生存中は startedAt + timeout + killGrace、
    /// 子の終了後は終了時刻 + stdinWriteGrace）を過ぎたら読み手がいないものと
    /// みなして書き込みを諦める。どの経路でも書き口はこの関数がちょうど 1 回閉じる。
    private static func writeStdin(_ data: Data, to fd: Int32, child: ChildRun,
                                   startedAt: TimeInterval, timeout: TimeInterval) {
        defer { close(fd) }
        data.withUnsafeBytes { raw in
            var written = 0
            while written < raw.count, let base = raw.baseAddress {
                child.lock.lock()
                let exited = child.exited
                let exitAt = child.exitAt
                child.lock.unlock()
                let deadline = exited ? exitAt + stdinWriteGrace
                                      : startedAt + timeout + killGrace
                let remaining = deadline - engineMonotonicNow()
                if remaining <= 0 { return }  // 期限: この先の入力を読む者はいない
                var pfd = pollfd(fd: fd, events: Int16(POLLOUT), revents: 0)
                let count = withUnsafeMutablePointer(to: &pfd) {
                    poll($0, 1, Int32(min(remaining, enginePipePollTick) * 1000))
                }
                if count < 0 {
                    if errno == EINTR { continue }
                    return
                }
                if count == 0 { continue }
                if pfd.revents & Int16(POLLNVAL) != 0 { return }
                let n = write(fd, base.advanced(by: written), raw.count - written)
                if n > 0 {
                    written += n
                } else if n == 0 {
                    return  // 0 バルトしか書けない: これ以上進まない
                } else if errno == EINTR || errno == EAGAIN {
                    continue  // nonblocking で書けないときは poll からやり直す
                } else {
                    if errno != EPIPE {  // EPIPE は子が stdin を読まずに終わった場合
                        let e = NSError(domain: NSPOSIXErrorDomain, code: Int(errno),
                            userInfo: [NSLocalizedDescriptionKey: String(cString: strerror(errno))])
                        child.lock.lock()
                        child.writeError = e
                        child.lock.unlock()
                    }
                    return
                }
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
            let original = try await getConfig()
            var edited = original
            mutate(&edited)
            let saved = try await applyChanges(from: original, to: edited)
            await Self.configLock.release()
            return saved
        } catch {
            await Self.configLock.release()
            throw error
        }
    }

    func saveChanges(from original: ConfigDocument, to edited: ConfigDocument) async throws -> ConfigDocument {
        await Self.configLock.acquire()
        do {
            let saved = try await applyChanges(from: original, to: edited)
            await Self.configLock.release()
            return saved
        } catch {
            await Self.configLock.release()
            throw error
        }
    }

    private func applyChanges(from original: ConfigDocument, to edited: ConfigDocument) async throws -> ConfigDocument {
        let input = try JSONSerialization.data(withJSONObject: ["original": original.object, "edited": edited.object])
        return try ConfigDocument(data: await run(["config", "--apply-json"], stdin: input))
    }

    func notificationCandidates(previous: Data?, current: Data, now: Date, notified: Set<String>) async throws -> [NotificationCandidate] {
        let currentObject = try JSONSerialization.jsonObject(with: current)
        let previousObject: Any = try previous.map { try JSONSerialization.jsonObject(with: $0) } ?? NSNull()
        let input = try JSONSerialization.data(withJSONObject: ["previous": previousObject, "current": currentObject,
            "now": now.timeIntervalSince1970, "notified": Array(notified)])
        struct Reply: Decodable { let candidates: [NotificationCandidate] }
        return try decode(Reply.self, from: await run(["notifications-plan"], stdin: input)).candidates
    }

    // MARK: - typed commands

    /// `quota-canvas detect --json`（各サービスで利用状況を 1 回取得するため遅い）
    func detect() async throws -> [DetectService] {
        try decode([DetectService].self, from: try await run(["detect", "--json"]))
    }

    /// `quota-canvas detect --json --cached`（常駐エンジン/前回 detect が書いた
    /// detect-cache.json を即返す。キャッシュが無い・壊れているときだけエンジン側で全取得する）
    func detectCached() async throws -> DetectCacheReply {
        try decode(DetectCacheReply.self, from: try await run(["detect", "--json", "--cached"]))
    }

    /// `quota-canvas config --json`
    func getConfig() async throws -> ConfigDocument {
        try ConfigDocument(data: try await run(["config", "--json"]))
    }

    /// `quota-canvas set-key <provider>`（キーは stdin で渡す。引数に載せない）
    func setKey(provider: String, key: String) async throws {
        _ = try await run(["set-key", provider], stdin: Data(key.trimmingCharacters(in: .whitespacesAndNewlines).utf8))
    }

    /// `quota-canvas muse-action-id [--refresh] [--chunks-stdin]`
    /// chunks を渡すと改行区切りで stdin に流す（ページが読み込んだ chunk パス）。
    /// timeout で子プロセスを打ち切る。stdout の ID 文字列を返す。
    func museActionId(refresh: Bool, chunks: [String]? = nil,
                      timeout: TimeInterval? = nil) async throws -> String {
        var args = ["muse-action-id"]
        if refresh { args.append("--refresh") }
        var stdinData: Data?
        if let chunks {
            args.append("--chunks-stdin")
            stdinData = Data(chunks.joined(separator: "\n").utf8)
        }
        let id = String(data: try await run(args, stdin: stdinData, timeout: timeout), encoding: .utf8)?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        guard !id.isEmpty else {
            throw EngineError(message: "muse action id is empty")
        }
        return id
    }

    /// `quota-canvas muse-ingest --status N`（本文は stdin で渡す）
    func museIngest(status: Int, body: String, timeout: TimeInterval? = nil) async throws {
        _ = try await run(["muse-ingest", "--status", String(status)],
                          stdin: Data(body.utf8), timeout: timeout)
    }

    /// `quota-canvas muse-ingest --clear`（muse-usage.json を消す）
    func museIngestClear() async throws {
        _ = try await run(["muse-ingest", "--clear"])
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

extension EngineClient: RoutingModeClient {}

private extension JSONDecoder {}
