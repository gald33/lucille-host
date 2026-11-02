import Foundation

/// Why the host is not connected, when the app knows. `run` is not started again until the pair
/// changes.
enum NotConnected: Equatable, Sendable {
    case notPaired
    case noGate
    case leaked
    case expired
    /// Lucille ended the pair.
    case ended
    /// Another command on this Mac forgot the pair while `run` ran.
    case forgotten
    /// An exit 20 whose reason this release does not know.
    case other
}

/// What needs the user, and stops the app from starting `run` until they act.
enum Attention: Equatable, Sendable {
    /// Five crashes in ten minutes.
    case keepsStopping
    /// Another `run` of the profile stayed running past a drain's bound.
    case anotherCopy
    case insecureKeyring
    case storeError
    case startupRefused
    case downgrade(lastVersion: String?)
    /// Exit 2: the app's own mistake against the host it ships.
    case appBug
    /// An object of another major version of the contract.
    case contract
    case severalPairs
    case pairUnusable
    /// `status --json` itself failed, with this code, or with no answer.
    case statusFailed(code: String?)

    /// Whether the note offers Try Again, which reads `status` again and starts `run` if it can.
    var offersTryAgain: Bool {
        switch self {
        case .keepsStopping, .anotherCopy, .storeError, .statusFailed:
            return true
        case .insecureKeyring, .startupRefused, .downgrade, .appBug, .contract, .severalPairs, .pairUnusable:
            return false
        }
    }

    /// For a store `status` could not read (`unreadable`'s code), or an exit 23.
    static func store(_ code: String) -> Attention {
        switch code {
        case "insecure_keyring":
            return .insecureKeyring
        case "pair_unusable":
            return .pairUnusable
        default:
            return .storeError
        }
    }
}

/// Why the supervisor stopped starting `run`.
enum Halt: Equatable, Sendable {
    case notConnected(NotConnected)
    case needsAttention(Attention)
    /// The app's own quit, or a logout, drained `run`.
    case quitDone
}

/// What the line under Needs attention says.
enum Problem: Equatable, Sendable {
    case halted(Attention)
    /// No `state` for 120 s: `run` is not stepping (spec/CLI.md §13.2). Shown only; nothing is
    /// stopped or restarted for it.
    case notResponding
    case policyInvalid
    /// The policy has been `checking` for more than 30 s.
    case policyStuck
    case clock(skew: Double?)
    case hubUnreachable(since: Date?)
    case readStuck
    case leftConnection(because: String)
}

/// The menu's status line, by priority (`RunSupervisor.health`).
enum Health: Equatable, Sendable {
    case notConnected(NotConnected?)
    case connecting
    /// A crash, and `run` starts again after its back-off.
    case startingAgain
    /// Exit 21: a `run` from before is still finishing.
    case finishingEarlier
    /// `run` drains, for a quit, a logout or a Disconnect.
    case stopping
    case needsAttention(Problem)
    case connectedWaiting
    case connected
}

/// Supervises `run --json-events --exit-on-stdin-eof` as the app's child (decisions/0002 §9,
/// spec/CLI.md §13). It is a value: inputs in, effects out. The app's glue runs each effect, sends
/// back what came of it, and sends `tick` at `nextWake`. Status reads carry the `epoch` they were
/// asked in, and an older one is dropped.
///
/// Two clocks come in with every input: `now`, which does not count time asleep (the glue passes
/// ProcessInfo's `systemUptime`), for the back-off, the crash window and the silence watch; and
/// `wall`, for `last_hub_contact`, which the host writes in wall time. After a wake, which shows as
/// the wall clock moving further than `now`, the silence and hub watches wait `wakeGrace`.
///
/// The rules it keeps, each a self-test check:
/// - `run` is started only as the answer to a `status` read it asked for, in the current epoch,
///   that reports the pair confirmed. A Disconnect, a pairing starting or stopping, or a quit
///   drops every pending restart.
/// - A quit during a Disconnect still runs `forget` once `run` has exited, before the reply.
/// - `resume` runs only when the user asks.
struct RunSupervisor: Equatable, Sendable {
    static let crashWindow: TimeInterval = 600
    static let crashLimit = 5
    /// A `run` ready this long starts the back-off from 2 s again.
    static let calmAfter: TimeInterval = 600
    static let stateSilence: TimeInterval = 120
    static let hubStale: TimeInterval = 120
    static let policyChecking: TimeInterval = 30
    /// The drain's bound for a quit: the longest operation (300 s), then 30 s for its reply.
    static let quitBound: TimeInterval = 330
    static let logoutBound: TimeInterval = 20
    static let afterKill: TimeInterval = 40
    static let afterStopped: TimeInterval = 10
    /// How long a `run` from before may stay running before the app says another copy runs.
    static let otherRunBound: TimeInterval = 330
    /// How long a drain runs before the finishing panel shows.
    static let panelDelay: TimeInterval = 0.5
    static let wakeGrace: TimeInterval = 120
    /// How much further than `now` the wall clock must move between two inputs to count as a wake.
    static let wakeJump: TimeInterval = 30

    /// The back-off before restart `attempt` (1, 2, 3 …): 2, 4, 8 … 256, then 300 s.
    static func backoff(attempt: Int) -> TimeInterval {
        if attempt <= 1 {
            return 2
        }
        if attempt >= 9 {
            return 300
        }
        return min(TimeInterval(1 << attempt), 300)
    }

    static func bound(_ kind: DrainKind) -> TimeInterval {
        kind == .logout ? logoutBound : quitBound
    }

    enum DrainKind: Equatable, Sendable {
        /// The app's own Quit, or an Apple-event quit that is not a logout.
        case quit
        /// A logout, restart or shutdown.
        case logout
        /// Disconnect from Lucille…: drain first, then `forget`.
        case disconnect
    }

    /// Why the supervisor asked for a `status` read.
    enum Why: Equatable, Sendable {
        case start
        case retry
        /// Exit 21: is a `run` still running?
        case otherRun(firstSeen: TimeInterval)
        /// An exit 0 or 20 with no `stopped`: where is the pair?
        case afterExit
    }

    /// What the supervisor knows of the `run` it started.
    struct Run: Equatable, Sendable {
        var startedAt: TimeInterval
        var readyAt: TimeInterval?
        var lastStateAt: TimeInterval?
        var state: RunState?
        var stopped: StopReason?
        var stoppedAt: TimeInterval?
        var policyCheckingSince: TimeInterval?
        var contractBroken = false
        var sigkillSent = false
    }

    struct Drain: Equatable, Sendable {
        var kind: DrainKind
        var since: TimeInterval
        var panelShown = false
        var killSentAt: TimeInterval?
        /// A quit came while a Disconnect drained: `forget` still runs, before the app replies.
        var thenForget = false
    }

    enum Phase: Equatable, Sendable {
        case idle
        case awaitingStatus(Why)
        case running(Run)
        case draining(Run, Drain)
        case backoff(until: TimeInterval)
        case otherRun(firstSeen: TimeInterval, retryAt: TimeInterval)
        case halted(Halt)
    }

    /// What an exit of `run` means (`classify`).
    enum Outcome: Equatable, Sendable {
        /// A drain the app asked for.
        case drainedAsAsked
        /// Restart with back-off, counted toward the five.
        case crash
        /// No `stopped` after 0 or 20: read `status` before acting.
        case askStatus
        /// Exit 21: read `status`, and wait while a `run` is still running.
        case otherRun
        case notConnected(NotConnected)
        case needsAttention(Attention)
    }

    enum Effect: Equatable, Sendable {
        /// Read `status --json`, and send back `statusRead` or `statusFailed`, with the epoch
        /// as it is after this step.
        case readStatus
        /// Start `run` with its lifeline, and send back `object`s and its `exited`.
        case spawnRun
        case closeLifeline
        /// Run `kill --json`: kills the operation's process group, and pauses (spec/CLI.md §13.4).
        case runKill
        /// SIGKILL `run`, the last step.
        case sigkillRun
        /// Run `forget --json`, now that a Disconnect has drained `run`.
        case runForget
        /// Run `resume --json`. Only the user's Resume asks for it.
        case runResume
        /// The app may now quit: reply to applicationShouldTerminate.
        case replyTerminate
        /// Turn sudden and automatic termination off (true) while a child lives, on again (false).
        case terminationGuard(Bool)
        case showFinishing(logout: Bool)
        /// The finishing panel says Stopping…, and its button is off.
        case finishingStopping
        case hideFinishing
        /// Lucille asked for a newer release: say so, once for the highest version asked.
        case tellHostTooOld(minVersion: String, hostVersion: String)
        /// One diagnostic line on standard error. It never holds a code or a SAS.
        case log(String)
    }

    enum Input: Equatable, Sendable {
        /// At launch, and when a pairing was confirmed: read `status`, and start `run` if the
        /// pair is confirmed.
        case start
        case statusRead(HostStatus)
        case statusFailed(VerbOutcome)
        /// `run` could not be started.
        case spawnFailed
        case object(HostObject)
        case exited(ChildExit)
        case tick
        /// A quit: the app's own (`.quit`), or a logout (`.logout`).
        case quit(DrainKind)
        /// The finishing panel's Stop now.
        case stopNow
        case disconnect
        /// A pairing started or stopped.
        case pairingStarted
        case tryAgain
        case userResume
    }

    private(set) var phase: Phase = .idle
    private(set) var epoch = 0
    private(set) var attempt = 0
    private(set) var crashes: [TimeInterval] = []
    private(set) var highestMinVersion: String?
    private(set) var graceUntil: TimeInterval?
    private var lastNow: TimeInterval?
    private var lastWall: Date?

    init() {}

    /// Whether a `run` child lives.
    var runAlive: Bool {
        switch phase {
        case .running, .draining:
            return true
        default:
            return false
        }
    }

    mutating func handle(_ input: Input, epoch inputEpoch: Int, now: TimeInterval, wall: Date) -> [Effect] {
        noteClocks(now: now, wall: wall)
        switch input {
        case .start:
            return start()
        case .statusRead(let status):
            return inputEpoch == epoch ? statusRead(status, now: now) : []
        case .statusFailed(let outcome):
            return inputEpoch == epoch ? statusFailed(outcome) : []
        case .spawnFailed:
            guard case .running = phase else {
                return []
            }
            var effects: [Effect] = [.terminationGuard(false), .log("run could not be started")]
            effects += crashed(now: now)
            return effects
        case .object(let object):
            return received(object, now: now)
        case .exited(let childExit):
            return exited(childExit, now: now)
        case .tick:
            return tick(now: now)
        case .quit(let kind):
            return quit(kind == .logout ? .logout : .quit, now: now)
        case .stopNow:
            return stopNow(now: now)
        case .disconnect:
            return disconnect(now: now)
        case .pairingStarted:
            return pairingStarted()
        case .tryAgain:
            return tryAgain()
        case .userResume:
            return [.runResume]
        }
    }

    /// When the glue should next send `tick`, on the `now` clock; nil when nothing waits.
    var nextWake: TimeInterval? {
        switch phase {
        case .backoff(let until):
            return until
        case .otherRun(_, let retryAt):
            return retryAt
        case .running(let run):
            guard let stoppedAt = run.stoppedAt, !run.sigkillSent else {
                return nil
            }
            return stoppedAt + RunSupervisor.afterStopped
        case .draining(let run, let drain):
            var times: [TimeInterval] = []
            if !drain.panelShown && drain.kind != .disconnect {
                times.append(drain.since + RunSupervisor.panelDelay)
            }
            if drain.killSentAt == nil {
                times.append(drain.since + RunSupervisor.bound(drain.kind))
            }
            if !run.sigkillSent {
                if let killedAt = drain.killSentAt {
                    times.append(killedAt + RunSupervisor.afterKill)
                }
                if let stoppedAt = run.stoppedAt {
                    times.append(stoppedAt + RunSupervisor.afterStopped)
                }
            }
            return times.min()
        case .idle, .awaitingStatus, .halted:
            return nil
        }
    }

    /// What an exit of `run` means: how it ended first, then its number checked against the
    /// `stopped` reason, then the reason within the number (spec/CLI.md §13.3, decisions/0002 §9).
    /// A number that disagrees with a reason this release knows is a crash.
    static func classify(exit: ChildEnd, stopped: StopReason?, asked: Bool) -> Outcome {
        guard case .exited(let code) = exit else {
            return .crash
        }
        if let stopped, let expected = stopped.exitCode, expected != code {
            return .crash
        }
        switch code {
        case 0:
            switch stopped {
            case .drained(let cause)?:
                if case .other = cause {
                    return .crash
                }
                return asked ? .drainedAsAsked : .crash
            case .forgotten?:
                return .notConnected(.forgotten)
            case .ended?:
                return .notConnected(.ended)
            case nil:
                return .askStatus
            default:
                // A reason this release does not know: by the exit code, a drain.
                return asked ? .drainedAsAsked : .crash
            }
        case 20:
            switch stopped {
            case .notPaired?:
                return .notConnected(.notPaired)
            case .noGate?:
                return .notConnected(.noGate)
            case .leaked?:
                return .notConnected(.leaked)
            case .expired?:
                return .notConnected(.expired)
            case .severalPairs?:
                return .needsAttention(.severalPairs)
            case .pairUnusable?:
                return .needsAttention(.pairUnusable)
            case nil:
                return .askStatus
            default:
                return .notConnected(.other)
            }
        case 21:
            return .otherRun
        case 23:
            if case .insecureKeyring? = stopped {
                return .needsAttention(.insecureKeyring)
            }
            return .needsAttention(.storeError)
        case 24:
            return .needsAttention(.startupRefused)
        case 25:
            if case .downgrade(let last)? = stopped {
                return .needsAttention(.downgrade(lastVersion: last))
            }
            return .needsAttention(.downgrade(lastVersion: nil))
        case 2:
            return .needsAttention(.appBug)
        default:
            // 1 (`failed`, a `store_error` after `ready` included), and any number this release
            // does not know.
            return .crash
        }
    }

    /// One diagnostic line for an exit: `run exited 0 (drained, stdin_end)`.
    static func describe(_ end: ChildEnd, stopped: StopReason?) -> String {
        let how: String
        switch end {
        case .exited(let code):
            how = "run exited \(code)"
        case .signaled(let number):
            how = "run ended by signal \(number)"
        }
        return how + " (" + (stopped?.words ?? "no stopped object") + ")"
    }

    /// The status line, by priority: a halt, a back-off, a `run` from before, then what `run`'s
    /// `state` says, or, while no `run` lives, what `status` says.
    func health(now: TimeInterval, wall: Date, status: HostStatus?) -> Health {
        switch phase {
        case .halted(.needsAttention(let attention)):
            return .needsAttention(.halted(attention))
        case .halted(.notConnected(let why)):
            return .notConnected(why)
        case .backoff:
            return .startingAgain
        case .otherRun, .awaitingStatus(.otherRun):
            return .finishingEarlier
        case .draining:
            return .stopping
        case .running(let run):
            return runHealth(run, now: now, wall: wall)
        case .idle, .awaitingStatus, .halted(.quitDone):
            return RunSupervisor.health(of: status)
        }
    }

    /// Whether the menu says Paused: what `run` last said while it runs, else what `status` said.
    func paused(status: HostStatus?) -> Bool {
        if case .running(let run) = phase, let state = run.state {
            return state.paused
        }
        return status?.paused ?? false
    }

    static func health(of status: HostStatus?) -> Health {
        guard let status else {
            return .notConnected(nil)
        }
        switch status.pair {
        case .noPair, .other:
            return .notConnected(nil)
        case .waiting, .sasReady, .sasPending, .confirmed:
            return .connecting
        case .expired:
            return .notConnected(.expired)
        case .refused:
            return .notConnected(.noGate)
        case .severalPairs:
            return .needsAttention(.halted(.severalPairs))
        case .unreadable(let code):
            return .needsAttention(.halted(Attention.store(code)))
        }
    }

    /// Whether `version` is a newer `MAJOR.MINOR.PATCH` than `other`.
    static func newer(_ version: String, than other: String?) -> Bool {
        guard let other else {
            return true
        }
        return triple(other).lexicographicallyPrecedes(triple(version))
    }

    private static func triple(_ version: String) -> [Int] {
        let parts = version.split(separator: ".").prefix(3).map { Int($0) ?? 0 }
        return parts + Array(repeating: 0, count: max(0, 3 - parts.count))
    }

    // MARK: Steps

    private mutating func noteClocks(now: TimeInterval, wall: Date) {
        if let then = lastNow, let wallThen = lastWall {
            let awake = now - then
            let passed = wall.timeIntervalSince(wallThen)
            if passed - awake > RunSupervisor.wakeJump {
                graceUntil = now + RunSupervisor.wakeGrace
            }
        }
        lastNow = now
        lastWall = wall
    }

    private mutating func start() -> [Effect] {
        switch phase {
        case .idle, .awaitingStatus, .halted(.notConnected), .halted(.quitDone):
            epoch += 1
            phase = .awaitingStatus(.start)
            return [.readStatus]
        case .running, .draining, .backoff, .otherRun, .halted(.needsAttention):
            return []
        }
    }

    private mutating func statusRead(_ status: HostStatus, now: TimeInterval) -> [Effect] {
        guard case .awaitingStatus(let why) = phase else {
            return []
        }
        switch status.pair {
        case .confirmed:
            switch why {
            case .otherRun(let firstSeen):
                if case .running = status.run {
                    if now - firstSeen >= RunSupervisor.otherRunBound {
                        phase = .halted(.needsAttention(.anotherCopy))
                        return []
                    }
                    attempt += 1
                    phase = .otherRun(firstSeen: firstSeen, retryAt: now + RunSupervisor.backoff(attempt: attempt))
                    return []
                }
                return spawn(now: now)
            case .afterExit:
                // It ended with no reason, and the pair is still confirmed: a crash.
                return crashed(now: now)
            case .start, .retry:
                return spawn(now: now)
            }
        case .unreadable(let code):
            phase = .halted(.needsAttention(Attention.store(code)))
        case .severalPairs:
            phase = .halted(.needsAttention(.severalPairs))
        case .noPair:
            phase = .halted(.notConnected(.notPaired))
        case .expired:
            phase = .halted(.notConnected(.expired))
        case .refused:
            phase = .halted(.notConnected(.noGate))
        case .waiting, .sasReady, .sasPending, .other:
            // Not confirmed yet: the pairing flow's business, and `run` waits for it.
            phase = .idle
        }
        return []
    }

    private mutating func statusFailed(_ outcome: VerbOutcome) -> [Effect] {
        guard case .awaitingStatus = phase else {
            return []
        }
        var code: String?
        if case .refused(let error) = outcome {
            code = error.code
        }
        phase = .halted(.needsAttention(.statusFailed(code: code)))
        return []
    }

    private mutating func spawn(now: TimeInterval) -> [Effect] {
        phase = .running(Run(startedAt: now))
        return [.spawnRun, .terminationGuard(true)]
    }

    private mutating func crashed(now: TimeInterval) -> [Effect] {
        crashes = crashes.filter { now - $0 < RunSupervisor.crashWindow } + [now]
        if crashes.count >= RunSupervisor.crashLimit {
            phase = .halted(.needsAttention(.keepsStopping))
            return []
        }
        attempt += 1
        phase = .backoff(until: now + RunSupervisor.backoff(attempt: attempt))
        return []
    }

    private mutating func received(_ object: HostObject, now: TimeInterval) -> [Effect] {
        switch phase {
        case .running(var run):
            let effects = note(object, in: &run, now: now)
            phase = .running(run)
            return effects
        case .draining(var run, let drain):
            let effects = note(object, in: &run, now: now)
            phase = .draining(run, drain)
            return effects
        default:
            return []
        }
    }

    private mutating func note(_ object: HostObject, in run: inout Run, now: TimeInterval) -> [Effect] {
        switch object {
        case .otherVersion:
            // Stop trusting the events, keep reading, and stop `run` (spec/CLI.md §13.2).
            if run.contractBroken {
                return []
            }
            run.contractBroken = true
            return [.closeLifeline]
        case .ready:
            run.readyAt = now
            return []
        case .state(let state):
            run.state = state
            run.lastStateAt = now
            if state.policy == .checking {
                if run.policyCheckingSince == nil {
                    run.policyCheckingSince = now
                }
            } else {
                run.policyCheckingSince = nil
            }
            return []
        case .stopped(let reason):
            run.stopped = reason
            run.stoppedAt = now
            return []
        case .hostTooOld(_, let least, let have):
            guard RunSupervisor.newer(least, than: highestMinVersion) else {
                return []
            }
            highestMinVersion = least
            return [.tellHostTooOld(minVersion: least, hostVersion: have)]
        default:
            return []
        }
    }

    private mutating func exited(_ childExit: ChildExit, now: TimeInterval) -> [Effect] {
        switch phase {
        case .draining(let run, let drain):
            let line = RunSupervisor.describe(childExit.end, stopped: run.stopped)
            switch drain.kind {
            case .quit, .logout:
                phase = .halted(.quitDone)
                if drain.thenForget {
                    // The Disconnect the quit came during is finished: `forget`, then the reply,
                    // which the app sends once `forget` has ended.
                    return [.terminationGuard(false), .log(line), .runForget, .replyTerminate]
                }
                return [.terminationGuard(false), .log(line), .hideFinishing, .replyTerminate]
            case .disconnect:
                epoch += 1
                phase = .idle
                return [.terminationGuard(false), .log(line), .runForget]
            }
        case .running(let run):
            var effects: [Effect] = [
                .terminationGuard(false), .log(RunSupervisor.describe(childExit.end, stopped: run.stopped)),
            ]
            if run.contractBroken {
                phase = .halted(.needsAttention(.contract))
                return effects
            }
            if let ready = run.readyAt, now - ready >= RunSupervisor.calmAfter {
                attempt = 0
            }
            var end = childExit.end
            if case .signaled = end, run.sigkillSent, let code = run.stopped?.exitCode {
                // The app killed it 10 s after its `stopped`: go by what that said.
                end = .exited(code)
            }
            switch RunSupervisor.classify(exit: end, stopped: run.stopped, asked: false) {
            case .crash:
                effects += crashed(now: now)
            case .askStatus:
                phase = .awaitingStatus(.afterExit)
                effects.append(.readStatus)
            case .otherRun:
                phase = .awaitingStatus(.otherRun(firstSeen: now))
                effects.append(.readStatus)
            case .notConnected(let why):
                phase = .halted(.notConnected(why))
            case .needsAttention(let attention):
                phase = .halted(.needsAttention(attention))
            case .drainedAsAsked:
                phase = .idle
            }
            return effects
        default:
            return [.terminationGuard(false)]
        }
    }

    private mutating func tick(now: TimeInterval) -> [Effect] {
        switch phase {
        case .backoff(let until):
            guard now >= until else {
                return []
            }
            phase = .awaitingStatus(.retry)
            return [.readStatus]
        case .otherRun(let firstSeen, let retryAt):
            guard now >= retryAt else {
                return []
            }
            phase = .awaitingStatus(.otherRun(firstSeen: firstSeen))
            return [.readStatus]
        case .running(var run):
            if let ready = run.readyAt, now - ready >= RunSupervisor.calmAfter {
                attempt = 0
            }
            guard let stoppedAt = run.stoppedAt, !run.sigkillSent, now - stoppedAt >= RunSupervisor.afterStopped else {
                return []
            }
            run.sigkillSent = true
            phase = .running(run)
            return [.sigkillRun]
        case .draining(var run, var drain):
            var effects: [Effect] = []
            if !drain.panelShown && drain.kind != .disconnect && now - drain.since >= RunSupervisor.panelDelay {
                drain.panelShown = true
                effects.append(.showFinishing(logout: drain.kind == .logout))
            }
            if drain.killSentAt == nil && now - drain.since >= RunSupervisor.bound(drain.kind) {
                drain.killSentAt = now
                effects.append(.runKill)
                if drain.panelShown {
                    effects.append(.finishingStopping)
                }
            }
            if !run.sigkillSent {
                if let killedAt = drain.killSentAt, now - killedAt >= RunSupervisor.afterKill {
                    run.sigkillSent = true
                } else if let stoppedAt = run.stoppedAt, now - stoppedAt >= RunSupervisor.afterStopped {
                    run.sigkillSent = true
                }
                if run.sigkillSent {
                    effects.append(.sigkillRun)
                }
            }
            phase = .draining(run, drain)
            return effects
        case .idle, .awaitingStatus, .halted:
            return []
        }
    }

    private mutating func quit(_ kind: DrainKind, now: TimeInterval) -> [Effect] {
        switch phase {
        case .running(let run):
            phase = .draining(run, Drain(kind: kind, since: now))
            return [.closeLifeline]
        case .draining(let run, var drain):
            // A quit while a Disconnect drains, or a logout while a quit drains: the app now
            // replies once `run` has exited, within the newer bound. A Disconnect is not dropped:
            // `forget` still runs before the reply.
            if drain.kind == .disconnect {
                drain.thenForget = true
                drain.kind = kind
                phase = .draining(run, drain)
            } else if kind == .logout && drain.kind == .quit {
                drain.kind = kind
                phase = .draining(run, drain)
            }
            return []
        default:
            epoch += 1
            phase = .halted(.quitDone)
            return [.replyTerminate]
        }
    }

    private mutating func stopNow(now: TimeInterval) -> [Effect] {
        guard case .draining(let run, var drain) = phase, drain.killSentAt == nil else {
            return []
        }
        drain.killSentAt = now
        phase = .draining(run, drain)
        return [.runKill, .finishingStopping]
    }

    private mutating func disconnect(now: TimeInterval) -> [Effect] {
        epoch += 1
        switch phase {
        case .running(let run):
            phase = .draining(run, Drain(kind: .disconnect, since: now))
            return [.closeLifeline]
        case .draining:
            return []
        default:
            phase = .idle
            return [.runForget]
        }
    }

    private mutating func pairingStarted() -> [Effect] {
        epoch += 1
        switch phase {
        case .backoff, .otherRun, .awaitingStatus, .halted(.notConnected):
            phase = .idle
        case .idle, .running, .draining, .halted(.needsAttention), .halted(.quitDone):
            break
        }
        return []
    }

    private mutating func tryAgain() -> [Effect] {
        guard case .halted(.needsAttention) = phase else {
            return []
        }
        crashes = []
        attempt = 0
        epoch += 1
        phase = .awaitingStatus(.retry)
        return [.readStatus]
    }

    private func runHealth(_ run: Run, now: TimeInterval, wall: Date) -> Health {
        let calm = graceUntil.map { now >= $0 } ?? true
        guard let ready = run.readyAt else {
            return .connecting
        }
        if calm && now - max(ready, run.lastStateAt ?? ready) >= RunSupervisor.stateSilence {
            return .needsAttention(.notResponding)
        }
        guard let state = run.state else {
            return .connecting
        }
        if state.policy == .invalid {
            return .needsAttention(.policyInvalid)
        }
        if state.policy == .checking, let since = run.policyCheckingSince, now - since > RunSupervisor.policyChecking {
            return .needsAttention(.policyStuck)
        }
        if case .skewed(let skew) = state.clock {
            return .needsAttention(.clock(skew: skew))
        }
        if calm {
            if let contact = state.lastHubContact {
                if wall.timeIntervalSince(contact) > RunSupervisor.hubStale {
                    return .needsAttention(.hubUnreachable(since: contact))
                }
            } else if now - ready > RunSupervisor.hubStale {
                return .needsAttention(.hubUnreachable(since: nil))
            }
        }
        if state.rooms.contains(where: { $0.state == .readStuck }) {
            return .needsAttention(.readStuck)
        }
        if let because = state.lastLeftBecause, state.rooms.isEmpty {
            return .needsAttention(.leftConnection(because: because))
        }
        if !state.confirmed {
            return .connecting
        }
        if state.rooms.contains(where: { $0.state == .reading }) {
            return .connected
        }
        return .connectedWaiting
    }
}
