import Foundation

#if canImport(Darwin)
import Darwin
#elseif canImport(Glibc)
import Glibc
#endif

/// What one run of the host's command line did.
struct CLIResult: Equatable, Sendable {
    /// The status when the program could not be started at all.
    static let notStarted: Int32 = -1
    /// The status when a stopped run did not end, even after SIGKILL, and the app gave up waiting.
    static let notFinished: Int32 = -2

    let status: Int32
    let stdout: String
    let stderr: String
    let timedOut: Bool
    /// The user cancelled the run, and the app stopped it.
    var cancelled = false

    var succeeded: Bool { status == 0 && !timedOut && !cancelled }
}

/// Lets the user stop a run before its time limit. `HostCLI.run` looks at it every tenth of a
/// second, and stops the program once it is cancelled.
final class CancelToken: @unchecked Sendable {
    private let lock = NSLock()
    private var cancelled = false

    var isCancelled: Bool {
        lock.lock()
        defer { lock.unlock() }
        return cancelled
    }

    func cancel() {
        lock.lock()
        cancelled = true
        lock.unlock()
    }
}

/// Runs the host's command line. It is the only program the app starts: the Python interpreter
/// inside the app, with `-I` (isolated: no PYTHON* variables, no user site-packages, no current
/// folder on the import path) and `-B` (no .pyc writes inside the signed app), running the
/// `lucille_host` package. Arguments go as an argv array, never through a shell. The environment
/// is fixed, not inherited. Every run has a time limit, and the user can cancel one.
struct HostCLI: Sendable {
    let python: URL
    let home: String
    let timeout: TimeInterval

    init(python: URL, home: String, timeout: TimeInterval = 30) {
        self.python = python
        self.home = home
        self.timeout = timeout
    }

    /// The interpreter inside an app bundle.
    static func python(inApp app: URL) -> URL {
        app.appendingPathComponent("Contents/Resources/python/bin/python3")
    }

    /// The home folder the host uses: HOME when it is an absolute path, as the host reads it.
    static func userHome() -> String {
        if let home = ProcessInfo.processInfo.environment["HOME"], home.hasPrefix("/") {
            return home
        }
        return NSHomeDirectory()
    }

    func arguments(for verb: [String]) -> [String] {
        ["-I", "-B", "-m", "lucille_host"] + verb
    }

    /// The whole environment a run gets. Nothing else from the app's own environment passes.
    func environment() -> [String: String] {
        var env = ["HOME": home, "PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8"]
        let own = ProcessInfo.processInfo.environment
        for key in ["USER", "LOGNAME", "TMPDIR"] {
            if let value = own[key] {
                env[key] = value
            }
        }
        return env
    }

    /// The flag file `lucille-host pause` writes for the default profile (`controls.py` and
    /// `paths.py`). The app only reads it, to show the state; `pause` and `resume` change it.
    var pauseFlag: URL {
        URL(fileURLWithPath: home)
            .appendingPathComponent("Library/Application Support/lucille-host/default/state/paused")
    }

    var isPaused: Bool { FileManager.default.fileExists(atPath: pauseFlag.path) }

    /// How many runs the app stopped waiting for have still not ended. One may hold the host's
    /// policy lock, and then every change to folders and abilities waits for it.
    static var stuckRuns: Int { leftBehind.running }

    private static let leftBehind = LeftBehind()

    /// Run one verb and wait for it, up to the time limit (`timeout`, or this command line's) or
    /// until `cancel` is cancelled. A run that is stopped gets SIGTERM, then SIGKILL 2 seconds
    /// later. If it still has not ended 2 seconds after that (a process stuck on a stalled network
    /// drive cannot end), the app stops waiting for it, counts it in `stuckRuns` until it ends,
    /// and the result has the status `notFinished`.
    func run(_ verb: [String], cancel: CancelToken? = nil, timeout: TimeInterval? = nil) -> CLIResult {
        let process = Process()
        process.executableURL = python
        process.arguments = arguments(for: verb)
        process.environment = environment()
        process.currentDirectoryURL = URL(fileURLWithPath: "/")
        process.standardInput = FileHandle.nullDevice
        let stdout = Pipe()
        let stderr = Pipe()
        process.standardOutput = stdout
        process.standardError = stderr
        let exited = DispatchSemaphore(value: 0)
        process.terminationHandler = { _ in exited.signal() }
        do {
            try process.run()
        } catch {
            return CLIResult(
                status: CLIResult.notStarted,
                stdout: "",
                stderr: "Loft could not start \(python.path): \(error.localizedDescription)",
                timedOut: false
            )
        }
        let out = PipeReader(stdout.fileHandleForReading)
        let err = PipeReader(stderr.fileHandleForReading)
        let deadline = Date().addingTimeInterval(timeout ?? self.timeout)
        var timedOut = false
        var cancelled = false
        while true {
            if cancel?.isCancelled == true {
                cancelled = true
                break
            }
            let step = min(0.1, max(0, deadline.timeIntervalSinceNow))
            if exited.wait(timeout: .now() + step) == .success {
                break
            }
            if Date() >= deadline {
                timedOut = true
                break
            }
        }
        if timedOut || cancelled {
            process.terminate()
            if exited.wait(timeout: .now() + 2) == .timedOut {
                kill(process.processIdentifier, SIGKILL)
                if exited.wait(timeout: .now() + 2) == .timedOut {
                    HostCLI.leftBehind.add(process)
                    return CLIResult(
                        status: CLIResult.notFinished,
                        stdout: "",
                        stderr: "",
                        timedOut: timedOut,
                        cancelled: cancelled
                    )
                }
            }
        }
        return CLIResult(
            status: process.terminationStatus,
            stdout: out.text(),
            stderr: err.text(),
            timedOut: timedOut,
            cancelled: cancelled
        )
    }
}

/// The runs the app stopped waiting for because they would not end.
private final class LeftBehind: @unchecked Sendable {
    private let lock = NSLock()
    private var processes: [Process] = []

    func add(_ process: Process) {
        lock.lock()
        processes.append(process)
        lock.unlock()
    }

    /// How many of them are still running. Those that have ended are forgotten.
    var running: Int {
        lock.lock()
        defer { lock.unlock() }
        processes.removeAll { !$0.isRunning }
        return processes.count
    }
}

/// Reads one pipe to its end on another thread, so that a child that writes a lot cannot fill the
/// pipe and wait forever.
private final class PipeReader: @unchecked Sendable {
    private var data = Data()
    private let done = DispatchSemaphore(value: 0)

    init(_ handle: FileHandle) {
        DispatchQueue.global(qos: .userInitiated).async {
            self.data = handle.readDataToEndOfFile()
            self.done.signal()
        }
    }

    func text() -> String {
        done.wait()
        return String(decoding: data, as: UTF8.self)
    }
}
