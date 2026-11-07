import Foundation

// Reading the host's JSON (spec/CLI.md, contract version 1): one object per line on standard
// output. The app decides only on the fields of these objects, never on the words in them, which
// are for people (spec/CLI.md §2). Foundation only, so the self-test runs it without a screen.

/// One JSON value, as the host writes it. Decoding keeps a boolean a boolean and a number a
/// number: `true` is never read as 1, nor 1 as `true`.
enum JSONValue: Equatable, Sendable, Codable {
    case null
    case bool(Bool)
    case number(Double)
    case string(String)
    case array([JSONValue])
    case object([String: JSONValue])

    init(from decoder: Decoder) throws {
        let single = try decoder.singleValueContainer()
        if single.decodeNil() {
            self = .null
        } else if let value = try? single.decode(Bool.self) {
            self = .bool(value)
        } else if let value = try? single.decode(Double.self) {
            self = .number(value)
        } else if let value = try? single.decode(String.self) {
            self = .string(value)
        } else if let value = try? single.decode([JSONValue].self) {
            self = .array(value)
        } else {
            self = .object(try single.decode([String: JSONValue].self))
        }
    }

    func encode(to encoder: Encoder) throws {
        var single = encoder.singleValueContainer()
        switch self {
        case .null:
            try single.encodeNil()
        case .bool(let value):
            try single.encode(value)
        case .number(let value):
            try single.encode(value)
        case .string(let value):
            try single.encode(value)
        case .array(let value):
            try single.encode(value)
        case .object(let value):
            try single.encode(value)
        }
    }
}

/// The fields of one JSON object, read by name and type. A field of the wrong type reads as
/// missing.
struct JSONFields: Equatable, Sendable {
    let values: [String: JSONValue]

    func string(_ key: String) -> String? {
        if case .string(let value)? = values[key] {
            return value
        }
        return nil
    }

    func bool(_ key: String) -> Bool? {
        if case .bool(let value)? = values[key] {
            return value
        }
        return nil
    }

    func number(_ key: String) -> Double? {
        if case .number(let value)? = values[key], value.isFinite {
            return value
        }
        return nil
    }

    /// A whole number a JSON reader keeps exactly (at most 2^53 - 1 either way).
    func int(_ key: String) -> Int? {
        guard let value = number(key), value == value.rounded(), abs(value) <= 9_007_199_254_740_991 else {
            return nil
        }
        return Int(value)
    }

    func isNull(_ key: String) -> Bool {
        if case .null? = values[key] {
            return true
        }
        return false
    }

    func object(_ key: String) -> JSONFields? {
        if case .object(let value)? = values[key] {
            return JSONFields(values: value)
        }
        return nil
    }

    func array(_ key: String) -> [JSONValue]? {
        if case .array(let value)? = values[key] {
            return value
        }
        return nil
    }

    /// A time the host wrote, in UTC to the second (`2026-11-01T12:00:00Z`, spec/CLI.md §4.1).
    func time(_ key: String) -> Date? {
        string(key).flatMap(HostJSON.time)
    }

    /// The strings of an array field; nil when it is missing or holds anything else.
    func strings(_ key: String) -> [String]? {
        guard let items = array(key) else {
            return nil
        }
        var found: [String] = []
        for item in items {
            guard case .string(let value) = item else {
                return nil
            }
            found.append(value)
        }
        return found
    }
}

/// An `error` object: what failed (`code`, which a shell branches on) and the host's sentence
/// (`message`, which a shell shows and never branches on).
struct HostError: Equatable, Sendable {
    let code: String
    let message: String
    let kind: String?
}

/// The `pair` type's states (spec/CLI.md §6, `pair`).
enum PairState: Equatable, Sendable {
    case noPair
    case accepted(name: String, clockSkewed: Bool)
    case waiting(name: String)
    case sasPending(name: String, sas: String, deadline: Date)
    case confirmed(name: String)
    case rejected(name: String, lucilleTold: Bool)
    /// A state this release does not know. The state is closed within version 1, so this is
    /// never shown and never acted on.
    case other(String)
}

/// Where the pair is, as `status` reports it, which never carries the SAS (spec/CLI.md §6,
/// `status`).
enum StatusPair: Equatable, Sendable {
    case noPair
    case waiting(name: String)
    case sasReady(name: String)
    case sasPending(name: String, deadline: Date)
    case confirmed(name: String)
    case refused(name: String)
    case expired(name: String, deadline: Date)
    case severalPairs
    /// The store that keeps the pair could not be read: `insecure_keyring`, `store_error` or
    /// `pair_unusable`.
    case unreadable(code: String)
    case other(String)
}

/// What the latest `run` last wrote, as `status` reports it.
enum StatusRun: Equatable, Sendable {
    case noRecord
    case running(pid: Int)
    case notSeen(pid: Int)
    case stopped
    case other(String)
}

/// The clock's state, in `status` and in `run`'s `state`. `skew` is in seconds, positive when
/// this computer's clock is behind the hub's.
enum ClockState: Equatable, Sendable {
    case ok(skew: Double)
    case skewed(skew: Double?)
    case unknown

    init(_ fields: JSONFields?) {
        switch fields?.string("state") ?? "" {
        case "ok":
            self = .ok(skew: fields?.number("skew_s") ?? 0)
        case "skewed":
            self = .skewed(skew: fields?.number("skew_s"))
        default:
            self = .unknown
        }
    }
}

/// The fields of `status` the app needs.
struct HostStatus: Equatable, Sendable {
    var hostVersion: String
    var paused: Bool
    /// The local policy is valid. An invalid one, or a policy object this release cannot read,
    /// is not.
    var policyOK: Bool
    /// The folders' aliases and the operations that run here, when the policy is valid.
    var folders: [String]
    var enabledOps: [String]
    var pair: StatusPair
    var run: StatusRun
    var lastHubContact: Date?
    var lastLeftBecause: String?
    var clock: ClockState
}

/// One work room `run` reads now (`state`'s `rooms`).
struct RunRoom: Equatable, Sendable {
    enum Reading: Equatable, Sendable {
        case reading
        case readStuck
        case other(String)
    }

    let name: String
    let state: Reading
}

/// What a running `run` sees now (spec/CLI.md §6, `state`).
struct RunState: Equatable, Sendable {
    enum Policy: Equatable, Sendable {
        case ok
        case checking
        /// Invalid, or a value this release does not know, which a reader treats as invalid
        /// (spec/CLI.md §12).
        case invalid
    }

    var pair: String
    var confirmed: Bool
    var paused: Bool
    var policy: Policy
    var lastHubContact: Date?
    var clock: ClockState
    var rooms: [RunRoom]
    var offersWaiting: Int
    var lastLeftBecause: String?
}

/// What began a drain of `run`.
enum DrainCause: Equatable, Sendable {
    case signal
    case stdinEnd
    case stdoutGone
    /// A cause this release does not know, which a reader treats as a drain it did not ask for
    /// (spec/CLI.md §12).
    case other(String)
}

/// Why `run` ended: `stopped`'s `reason` (spec/CLI.md §6 and §13.3).
enum StopReason: Equatable, Sendable {
    case drained(cause: DrainCause)
    case forgotten
    case ended
    case notPaired
    case noGate
    case severalPairs
    case pairUnusable
    case leaked
    case expired
    case alreadyRunning
    case insecureKeyring
    case storeError
    case startupRefused
    case downgrade(lastVersion: String)
    case failed(code: String)
    /// A reason this release does not know: a reader goes by the exit code (spec/CLI.md §12).
    case other(String)

    /// The exit code the contract gives this reason; nil for a reason this release does not know.
    var exitCode: Int32? {
        switch self {
        case .drained, .forgotten, .ended:
            return 0
        case .notPaired, .noGate, .severalPairs, .pairUnusable, .leaked, .expired:
            return 20
        case .alreadyRunning:
            return 21
        case .insecureKeyring, .storeError:
            return 23
        case .startupRefused:
            return 24
        case .downgrade:
            return 25
        case .failed:
            return 1
        case .other:
            return nil
        }
    }

    /// The reason in the contract's words, for a diagnostic line.
    var words: String {
        switch self {
        case .drained(let cause):
            switch cause {
            case .signal:
                return "drained, signal"
            case .stdinEnd:
                return "drained, stdin_end"
            case .stdoutGone:
                return "drained, stdout_gone"
            case .other(let word):
                return "drained, \(word)"
            }
        case .forgotten:
            return "forgotten"
        case .ended:
            return "ended"
        case .notPaired:
            return "not_paired"
        case .noGate:
            return "no_gate"
        case .severalPairs:
            return "several_pairs"
        case .pairUnusable:
            return "pair_unusable"
        case .leaked:
            return "leaked"
        case .expired:
            return "expired"
        case .alreadyRunning:
            return "already_running"
        case .insecureKeyring:
            return "insecure_keyring"
        case .storeError:
            return "store_error"
        case .startupRefused:
            return "startup_refused"
        case .downgrade:
            return "downgrade"
        case .failed(let code):
            return "failed, \(code)"
        case .other(let word):
            return word
        }
    }
}

/// How a request ended (spec/CLI.md §6, `request`, `finished`). The status is closed within
/// version 1; the codes of `refused` and `failed` are the protocol's, closed too.
enum RequestEnd: Equatable, Sendable {
    /// It ran, and its reply is built.
    case done
    case refused(String)
    /// It was decided to run and did not finish: `timeout`, `killed_locally` or `interrupted`.
    case failed(String)
    /// Its work room was given up while it was decided or ran: it gets no reply.
    case abandoned
}

/// The step a request is at (spec/CLI.md §6, `request`). What it asked for is Lucille's text, as
/// the host wrote it: only `RequestLog` keeps it, and only the Activity panel shows it.
enum RequestPhase: Equatable, Sendable {
    /// Written to the audit log, before it is decided. Each field is nil when the host wrote
    /// null: an op that is not an operation's name, or a path it would not show.
    case received(op: String?, root: String?, path: String?)
    /// To run, or refused with the protocol's code.
    case decided(run: Bool, code: String?)
    /// The process it runs is about to start: only for an operation that starts one.
    case started
    case finished(RequestEnd)
    /// A phase this release does not know, which a reader ignores (spec/CLI.md §12).
    case other(String)
}

/// One `request` object of `run`'s events: Lucille's id for the job, and its step.
struct RequestEvent: Equatable, Sendable {
    let jobId: String
    let phase: RequestPhase
}

/// What `forget` did (spec/CLI.md §6, `forget`).
enum ForgetResult: Equatable, Sendable {
    case forgotten(name: String, lucilleTold: Bool)
    case left(name: String)
    case other(String)
}

/// One object of the host's JSON output, for the types the app uses. Any other type is
/// `unknown`, which a reader ignores (spec/CLI.md §2).
enum HostObject: Equatable, Sendable {
    /// An object of another major version of the contract: a reader stops trusting what it reads.
    case otherVersion(Int)
    /// A pairing code's pair name. The code's `note` is not kept: whoever made the code chose it.
    case pairCode(name: String)
    case policyReset
    case pair(PairState)
    case error(HostError)
    case status(HostStatus)
    case forget(ForgetResult)
    case controls(paused: Bool)
    case ready(pair: String, hostVersion: String)
    case state(RunState)
    case request(RequestEvent)
    case hostTooOld(jobId: String, minVersion: String, hostVersion: String)
    case stopped(StopReason)
    /// One line of the audit log, as `log` prints it, which a shell shows as it is or not at all
    /// (spec/CLI.md §6, `log_line`).
    case logLine(String)
    /// This release's version, and the version of the contract it writes (`--json --version`).
    case version(hostVersion: String, contract: String)
    /// One operation of `ops list`, or the one `ops enable` or `ops disable` changed, as it now
    /// is: the menu's ability, in the core's words. Activity says each request with its summary.
    case op(Ability)
    /// `ops disable` took a name this release does not have out of the policy.
    case opRemoved(name: String)
    /// One folder of `roots list`, or the one `roots add` added.
    case root(Folder)
    /// The folder `roots remove` took out, by its alias.
    case rootRemoved(alias: String)
    /// Whether the host can open one folder (`roots check`).
    case rootCheck(FolderCheck)
    /// The alias the host suggests for a folder (`roots add --suggest-alias`).
    case aliasSuggestion(String)
    case unknown(type: String)

    /// The types this release reads. Every other type decodes as `unknown`.
    static let knownTypes: Set<String> = [
        "pair_code", "policy_reset", "pair", "error", "status", "forget", "controls", "ready",
        "state", "request", "host_too_old", "stopped", "log_line", "version", "op", "op_removed",
        "root", "root_removed", "root_check", "alias_suggestion",
    ]
}

/// Splits a stream into lines and reads each as one object, as spec/CLI.md §4.1 and §13.2 frame
/// it. A line of `run`'s events is at most 16384 bytes, its newline included: a longer one is
/// dropped while it streams, never kept whole. A short verb's output is read whole, with no bound
/// (`limit` nil). A line that ends with a carriage return, or is not one JSON object, is dropped
/// too, and so is a last fragment with no newline. Each dropped line becomes a note for
/// diagnostics, which never holds the line itself.
struct JSONLines: Sendable {
    /// The bound on a line of `run`'s events, its newline included (spec/CLI.md §13.2).
    static let maxLineBytes = 16_384

    enum Item: Equatable, Sendable {
        case object(HostObject)
        case unusable(String)
    }

    /// The most bytes a line may have, its newline included; nil for no bound.
    let limit: Int?
    private var line: [UInt8] = []
    private var overlong = false

    init(limit: Int? = JSONLines.maxLineBytes) {
        self.limit = limit
    }

    mutating func feed(_ data: Data) -> [Item] {
        var items: [Item] = []
        for byte in data {
            if byte == 0x0A {
                items.append(endLine())
            } else if overlong {
                continue
            } else if let limit, line.count >= limit - 1 {
                overlong = true
                line.removeAll()
            } else {
                line.append(byte)
            }
        }
        return items
    }

    /// The end of the stream: a fragment with no newline is discarded.
    mutating func finish() -> [Item] {
        defer {
            line.removeAll()
            overlong = false
        }
        if line.isEmpty && !overlong {
            return []
        }
        return [.unusable("a last line with no newline, discarded")]
    }

    private mutating func endLine() -> Item {
        defer {
            line.removeAll(keepingCapacity: true)
            overlong = false
        }
        if overlong {
            return .unusable("a line longer than \(limit ?? 0) bytes")
        }
        if line.last == 0x0D {
            return .unusable("a line that ends with a carriage return")
        }
        return HostObject.parse(line: Data(line))
    }
}

extension HostObject {
    /// One line, without its newline, read as one object.
    static func parse(line: Data) -> JSONLines.Item {
        guard let value = try? JSONDecoder().decode(JSONValue.self, from: line),
              case .object(let map) = value
        else {
            return .unusable("a line that is not a JSON object")
        }
        let fields = JSONFields(values: map)
        guard let version = fields.int("v") else {
            return .unusable("an object with no version")
        }
        if version != 1 {
            return .object(.otherVersion(version))
        }
        guard let type = fields.string("type") else {
            return .unusable("an object with no type")
        }
        guard let object = HostObject(type: type, fields) else {
            return .unusable("a \(type) object whose fields this release cannot read")
        }
        return .object(object)
    }

    /// The object of type `type`; nil when a field this release needs is missing or of the wrong
    /// type, which the host's schemas never allow.
    init?(type: String, _ fields: JSONFields) {
        switch type {
        case "pair_code":
            guard let name = fields.string("name") else {
                return nil
            }
            self = .pairCode(name: name)
        case "policy_reset":
            self = .policyReset
        case "pair":
            guard let state = HostObject.readPair(fields) else {
                return nil
            }
            self = .pair(state)
        case "error":
            guard let code = fields.string("code"), let message = fields.string("message") else {
                return nil
            }
            self = .error(HostError(code: code, message: message, kind: fields.string("kind")))
        case "status":
            guard let status = HostObject.readStatus(fields) else {
                return nil
            }
            self = .status(status)
        case "forget":
            guard let result = HostObject.readForget(fields) else {
                return nil
            }
            self = .forget(result)
        case "controls":
            guard let paused = fields.bool("paused") else {
                return nil
            }
            self = .controls(paused: paused)
        case "ready":
            guard let pair = fields.string("pair"), let version = fields.string("host_version") else {
                return nil
            }
            self = .ready(pair: pair, hostVersion: version)
        case "state":
            guard let state = HostObject.readRunState(fields) else {
                return nil
            }
            self = .state(state)
        case "request":
            guard let job = fields.string("job_id"), let phase = HostObject.readRequest(fields) else {
                return nil
            }
            self = .request(RequestEvent(jobId: job, phase: phase))
        case "host_too_old":
            // The minimum is Lucille's, and the update notice shows it: read only in the form the
            // protocol gives it.
            guard let job = fields.string("job_id"), let least = fields.string("min_host_version"),
                  HostObject.isMinHostVersion(least), let version = fields.string("host_version")
            else {
                return nil
            }
            self = .hostTooOld(jobId: job, minVersion: least, hostVersion: version)
        case "stopped":
            guard let reason = HostObject.readStopReason(fields) else {
                return nil
            }
            self = .stopped(reason)
        case "log_line":
            guard let text = fields.string("text") else {
                return nil
            }
            self = .logLine(text)
        case "version":
            guard let version = fields.string("host_version"), let contract = fields.string("contract") else {
                return nil
            }
            self = .version(hostVersion: version, contract: contract)
        case "op":
            guard let ability = HostObject.readOp(fields) else {
                return nil
            }
            self = .op(ability)
        case "op_removed":
            guard let name = fields.string("name") else {
                return nil
            }
            self = .opRemoved(name: name)
        case "root":
            guard let alias = fields.string("alias"), let path = fields.string("path"),
                  let broad = fields.bool("broad")
            else {
                return nil
            }
            self = .root(Folder(alias: alias, path: path, broad: broad))
        case "root_removed":
            guard let alias = fields.string("alias") else {
                return nil
            }
            self = .rootRemoved(alias: alias)
        case "root_check":
            guard let check = HostObject.readFolderCheck(fields) else {
                return nil
            }
            self = .rootCheck(check)
        case "alias_suggestion":
            // The name dialog offers it to the user: read only in the form the host's aliases take.
            guard let alias = fields.string("alias"), Alias.isValid(alias) else {
                return nil
            }
            self = .aliasSuggestion(alias)
        default:
            self = .unknown(type: type)
        }
    }

    /// Whether `text` is a SAS as the host writes one: four digits, `-`, four digits.
    static func isSAS(_ text: String) -> Bool {
        let scalars = Array(text.unicodeScalars)
        guard scalars.count == 9 else {
            return false
        }
        let digits: ClosedRange<Unicode.Scalar> = "0"..."9"
        for (index, scalar) in scalars.enumerated() {
            if index == 4 {
                if scalar != "-" {
                    return false
                }
            } else if !digits.contains(scalar) {
                return false
            }
        }
        return true
    }

    /// Whether `text` is a release in the form a request's `min_host_version` takes (spec/PROTOCOL.md
    /// §10, `request.schema.json`): MAJOR.MINOR.PATCH, each part ASCII digits, at most 9 of them,
    /// with no leading zero. Lucille sends it, and the update notice shows it (decisions/0002 §16).
    static func isMinHostVersion(_ text: String) -> Bool {
        let digits: ClosedRange<Unicode.Scalar> = "0"..."9"
        var parts = 1
        var length = 0
        var leadingZero = false
        for scalar in text.unicodeScalars {
            if scalar == "." {
                guard length > 0, parts < 3 else {
                    return false
                }
                parts += 1
                length = 0
                leadingZero = false
            } else if digits.contains(scalar) {
                guard !leadingZero, length < 9 else {
                    return false
                }
                leadingZero = length == 0 && scalar == "0"
                length += 1
            } else {
                return false
            }
        }
        return parts == 3 && length > 0
    }

    private static func readPair(_ fields: JSONFields) -> PairState? {
        guard let state = fields.string("state") else {
            return nil
        }
        if state == "none" {
            return .noPair
        }
        let name = fields.string("name")
        switch state {
        case "accepted":
            guard let name, let skewed = fields.bool("clock_skewed") else {
                return nil
            }
            return .accepted(name: name, clockSkewed: skewed)
        case "waiting":
            guard let name else {
                return nil
            }
            return .waiting(name: name)
        case "sas_pending":
            // A SAS of another shape, or no deadline, is never shown.
            guard let name, let sas = fields.string("sas"), isSAS(sas), let deadline = fields.time("deadline") else {
                return nil
            }
            return .sasPending(name: name, sas: sas, deadline: deadline)
        case "confirmed":
            guard let name else {
                return nil
            }
            return .confirmed(name: name)
        case "rejected":
            guard let name, let told = fields.bool("lucille_told") else {
                return nil
            }
            return .rejected(name: name, lucilleTold: told)
        default:
            return .other(state)
        }
    }

    private static func readStatusPair(_ fields: JSONFields) -> StatusPair? {
        guard let state = fields.string("state") else {
            return nil
        }
        let name = fields.string("name")
        switch state {
        case "none":
            return .noPair
        case "waiting":
            return name.map { StatusPair.waiting(name: $0) }
        case "sas_ready":
            return name.map { StatusPair.sasReady(name: $0) }
        case "sas_pending":
            guard let name, let deadline = fields.time("deadline") else {
                return nil
            }
            return .sasPending(name: name, deadline: deadline)
        case "confirmed":
            return name.map { StatusPair.confirmed(name: $0) }
        case "refused":
            return name.map { StatusPair.refused(name: $0) }
        case "expired":
            guard let name, let deadline = fields.time("deadline") else {
                return nil
            }
            return .expired(name: name, deadline: deadline)
        case "several_pairs":
            return .severalPairs
        case "unreadable":
            return fields.string("code").map { StatusPair.unreadable(code: $0) }
        default:
            return .other(state)
        }
    }

    private static func readStatusRun(_ fields: JSONFields) -> StatusRun? {
        guard let state = fields.string("state") else {
            return nil
        }
        switch state {
        case "none":
            return .noRecord
        case "running":
            return fields.int("pid").map { StatusRun.running(pid: $0) }
        case "not_seen":
            return fields.int("pid").map { StatusRun.notSeen(pid: $0) }
        case "stopped":
            return .stopped
        default:
            return .other(state)
        }
    }

    private static func readStatus(_ fields: JSONFields) -> HostStatus? {
        guard let version = fields.string("host_version"),
              let paused = fields.bool("paused"),
              let policy = fields.object("policy"),
              let pairFields = fields.object("pair"), let pair = readStatusPair(pairFields),
              let runFields = fields.object("run"), let run = readStatusRun(runFields)
        else {
            return nil
        }
        let policyOK = policy.string("state") == "ok"
        return HostStatus(
            hostVersion: version,
            paused: paused,
            policyOK: policyOK,
            folders: policyOK ? policy.strings("folders") ?? [] : [],
            enabledOps: policyOK ? policy.strings("enabled") ?? [] : [],
            pair: pair,
            run: run,
            lastHubContact: fields.time("last_hub_contact"),
            lastLeftBecause: fields.string("last_left_because"),
            clock: ClockState(fields.object("clock"))
        )
    }

    private static func readRunState(_ fields: JSONFields) -> RunState? {
        guard let pair = fields.string("pair"),
              let confirmed = fields.bool("confirmed"),
              let paused = fields.bool("paused"),
              let roomValues = fields.array("rooms")
        else {
            return nil
        }
        let policy: RunState.Policy
        switch fields.string("policy") ?? "" {
        case "ok":
            policy = .ok
        case "checking":
            policy = .checking
        default:
            policy = .invalid
        }
        var rooms: [RunRoom] = []
        for value in roomValues {
            guard case .object(let map) = value else {
                return nil
            }
            let room = JSONFields(values: map)
            guard let name = room.string("name"), let state = room.string("state") else {
                return nil
            }
            let reading: RunRoom.Reading
            switch state {
            case "reading":
                reading = .reading
            case "read_stuck":
                reading = .readStuck
            default:
                reading = .other(state)
            }
            rooms.append(RunRoom(name: name, state: reading))
        }
        return RunState(
            pair: pair,
            confirmed: confirmed,
            paused: paused,
            policy: policy,
            lastHubContact: fields.time("last_hub_contact"),
            clock: ClockState(fields.object("clock")),
            rooms: rooms,
            offersWaiting: fields.int("offers_waiting") ?? 0,
            lastLeftBecause: fields.string("last_left_because")
        )
    }

    private static func readStopReason(_ fields: JSONFields) -> StopReason? {
        guard let reason = fields.string("reason") else {
            return nil
        }
        switch reason {
        case "drained":
            let cause: DrainCause
            switch fields.string("cause") ?? "" {
            case "signal":
                cause = .signal
            case "stdin_end":
                cause = .stdinEnd
            case "stdout_gone":
                cause = .stdoutGone
            case let other:
                cause = .other(other)
            }
            return .drained(cause: cause)
        case "forgotten":
            return .forgotten
        case "ended":
            return .ended
        case "not_paired":
            return .notPaired
        case "no_gate":
            return .noGate
        case "several_pairs":
            return .severalPairs
        case "pair_unusable":
            return .pairUnusable
        case "leaked":
            return .leaked
        case "expired":
            return .expired
        case "already_running":
            return .alreadyRunning
        case "insecure_keyring":
            return .insecureKeyring
        case "store_error":
            return .storeError
        case "startup_refused":
            return .startupRefused
        case "downgrade":
            return .downgrade(lastVersion: fields.string("last_version") ?? "")
        case "failed":
            return .failed(code: fields.string("code") ?? "")
        default:
            return .other(reason)
        }
    }

    /// A request's step. A phase this release does not know is `other`, which a reader ignores;
    /// a decision or a status outside their closed sets, or a refusal or failure with no code, is
    /// a request whose fields this release cannot read (spec/CLI.md §12).
    private static func readRequest(_ fields: JSONFields) -> RequestPhase? {
        guard let phase = fields.string("phase") else {
            return nil
        }
        switch phase {
        case "received":
            return .received(op: fields.string("op"), root: fields.string("root"), path: fields.string("path"))
        case "decided":
            switch fields.string("decision") ?? "" {
            case "run":
                return .decided(run: true, code: nil)
            case "refused":
                return fields.string("code").map { RequestPhase.decided(run: false, code: $0) }
            default:
                return nil
            }
        case "started":
            return .started
        case "finished":
            switch fields.string("status") ?? "" {
            case "done":
                return .finished(.done)
            case "refused":
                return fields.string("code").map { RequestPhase.finished(.refused($0)) }
            case "failed":
                return fields.string("code").map { RequestPhase.finished(.failed($0)) }
            case "abandoned":
                return .finished(.abandoned)
            default:
                return nil
            }
        default:
            return .other(phase)
        }
    }

    /// One operation (spec/CLI.md §6, `op`). Its state is closed within version 1: a state this
    /// release does not know, or a field its state needs that is missing or of another type, makes
    /// the object one this release cannot read. So the switch's position and the question before an
    /// ability that sends file contents come only from the host's booleans, never from a guess.
    private static func readOp(_ fields: JSONFields) -> Ability? {
        guard let name = fields.string("name"), let state = fields.string("state"),
              let enabled = fields.bool("enabled")
        else {
            return nil
        }
        let read: Ability.State
        switch state {
        case "enabled":
            read = .enabled
        case "disabled":
            read = .disabled
        case "disabled_by_upgrade":
            read = .disabledByUpgrade
        case "unavailable":
            guard let missing = fields.string("missing_executable") else {
                return nil
            }
            read = .unavailable(missingExecutable: missing)
        case "not_in_catalog":
            // A name this release does not have: the core has no words for it.
            return Ability(
                name: name, state: .notInCatalog, enabled: enabled, summary: nil, sends: nil,
                sendsFileContent: false, diagnostic: false
            )
        default:
            return nil
        }
        guard let summary = fields.string("summary"), let sends = fields.string("sends"),
              let content = fields.bool("sends_file_content"), let diagnostic = fields.bool("diagnostic")
        else {
            return nil
        }
        return Ability(
            name: name, state: read, enabled: enabled, summary: summary, sends: sends,
            sendsFileContent: content, diagnostic: diagnostic
        )
    }

    /// One folder's check (spec/CLI.md §6, `root_check`). Its state is closed within version 1, as
    /// an operation's is.
    private static func readFolderCheck(_ fields: JSONFields) -> FolderCheck? {
        guard let alias = fields.string("alias"), let path = fields.string("path"),
              let state = fields.string("state")
        else {
            return nil
        }
        let read: FolderCheck.State
        switch state {
        case "ok":
            read = .ok
        case "denied_by_macos":
            read = .deniedByMacOS
        case "denied":
            read = .denied
        case "missing":
            read = .missing
        case "error":
            guard let code = fields.string("errno") else {
                return nil
            }
            read = .error(errno: code)
        default:
            return nil
        }
        return FolderCheck(alias: alias, path: path, state: read)
    }

    private static func readForget(_ fields: JSONFields) -> ForgetResult? {
        guard let state = fields.string("state") else {
            return nil
        }
        switch state {
        case "forgotten":
            guard let name = fields.string("name"), let told = fields.bool("lucille_told") else {
                return nil
            }
            return .forgotten(name: name, lucilleTold: told)
        case "left":
            return fields.string("name").map { ForgetResult.left(name: $0) }
        default:
            return .other(state)
        }
    }
}

/// What a short verb did, read from its exit and its objects (spec/CLI.md §4.3). How it ended
/// comes first: a signal is never read as an exit code.
enum VerbOutcome: Equatable, Sendable {
    /// Exit 0, with the objects it wrote.
    case done([HostObject])
    /// Exit 1 or 2, with an `error` object last.
    case refused(HostError)
    /// A signal ended it, or it exited 1 or 2 with no `error` object last, which means the same.
    case signaled
    case timedOut
    case cancelled
    /// The interpreter could not be started.
    case notStarted
    /// It was stopped, and had still not ended when the app stopped waiting.
    case notFinished
    /// Any other exit status.
    case otherExit(Int32)

    init(_ result: CLIResult) {
        if result.status == CLIResult.notFinished {
            self = .notFinished
        } else if result.timedOut {
            self = .timedOut
        } else if result.cancelled {
            self = .cancelled
        } else if result.status == CLIResult.notStarted {
            self = .notStarted
        } else if result.terminationReason == .uncaughtSignal {
            self = .signaled
        } else {
            let objects = HostJSON.objects(inOutput: result.stdout)
            switch result.status {
            case 0:
                self = .done(objects)
            case 1, 2:
                if case .error(let error)? = objects.last {
                    self = .refused(error)
                } else {
                    self = .signaled
                }
            default:
                self = .otherExit(result.status)
            }
        }
    }

    /// The `status` object of a `status --json` that worked.
    var status: HostStatus? {
        guard case .done(let objects) = self else {
            return nil
        }
        for object in objects {
            if case .status(let status) = object {
                return status
            }
        }
        return nil
    }

    /// The last `pair` object of a pairing verb that worked.
    var pair: PairState? {
        guard case .done(let objects) = self else {
            return nil
        }
        for object in objects.reversed() {
            if case .pair(let state) = object {
                return state
            }
        }
        return nil
    }

    /// The `controls` object of `pause`, `resume` or `kill`.
    var paused: Bool? {
        guard case .done(let objects) = self else {
            return nil
        }
        for object in objects {
            if case .controls(let paused) = object {
                return paused
            }
        }
        return nil
    }

    /// The `forget` object of `forget --json`.
    var forget: ForgetResult? {
        guard case .done(let objects) = self else {
            return nil
        }
        for object in objects {
            if case .forget(let result) = object {
                return result
            }
        }
        return nil
    }

    /// The lines of `log --json`, oldest first, as the host wrote them; none when the log is
    /// empty, or the verb did not work.
    var logLines: [String] {
        guard case .done(let objects) = self else {
            return []
        }
        var lines: [String] = []
        for object in objects {
            if case .logLine(let text) = object {
                lines.append(text)
            }
        }
        return lines
    }

    /// Each operation's summary from `ops list --json`, by its name, in the core's words (spec/CLI.md
    /// §8). A policy entry the catalog does not have has none.
    var summaries: [String: String] {
        var found: [String: String] = [:]
        for ability in abilities {
            if let summary = ability.summary {
                found[ability.name] = summary
            }
        }
        return found
    }

    /// The release of `--json --version`.
    var hostVersion: String? {
        guard case .done(let objects) = self else {
            return nil
        }
        for object in objects {
            if case .version(let version, _) = object {
                return version
            }
        }
        return nil
    }

    /// Each operation of `ops list --json`, in the host's order: the catalog's, then the policy's
    /// names this release does not have (spec/CLI.md §7). None when the verb did not work.
    var abilities: [Ability] {
        guard case .done(let objects) = self else {
            return []
        }
        var found: [Ability] = []
        for object in objects {
            if case .op(let ability) = object {
                found.append(ability)
            }
        }
        return found
    }

    /// The operation as it is once `ops enable` or `ops disable` made the change: a switch follows
    /// this, not what the app asked for (spec/CLI.md §7).
    var ability: Ability? {
        guard case .done(let objects) = self else {
            return nil
        }
        for object in objects.reversed() {
            if case .op(let ability) = object {
                return ability
            }
        }
        return nil
    }

    /// The name `ops disable` took out of the policy, which this release does not have.
    var removedOp: String? {
        guard case .done(let objects) = self else {
            return nil
        }
        for object in objects {
            if case .opRemoved(let name) = object {
                return name
            }
        }
        return nil
    }

    /// Each folder of `roots list --json`, by alias. None when the verb did not work.
    var folders: [Folder] {
        guard case .done(let objects) = self else {
            return []
        }
        var found: [Folder] = []
        for object in objects {
            if case .root(let folder) = object {
                found.append(folder)
            }
        }
        return found
    }

    /// The folder `roots add --json` added.
    var folder: Folder? {
        guard case .done(let objects) = self else {
            return nil
        }
        for object in objects.reversed() {
            if case .root(let folder) = object {
                return folder
            }
        }
        return nil
    }

    /// The alias `roots remove --json` took out.
    var removedFolder: String? {
        guard case .done(let objects) = self else {
            return nil
        }
        for object in objects {
            if case .rootRemoved(let alias) = object {
                return alias
            }
        }
        return nil
    }

    /// Each folder's check of `roots check --json`, by alias.
    var folderChecks: [FolderCheck] {
        guard case .done(let objects) = self else {
            return []
        }
        var found: [FolderCheck] = []
        for object in objects {
            if case .rootCheck(let check) = object {
                found.append(check)
            }
        }
        return found
    }

    /// The alias `roots add --suggest-alias --json` suggests.
    var aliasSuggestion: String? {
        guard case .done(let objects) = self else {
            return nil
        }
        for object in objects {
            if case .aliasSuggestion(let alias) = object {
                return alias
            }
        }
        return nil
    }

    /// The `error` object of a refusal, and of nothing else.
    var error: HostError? {
        if case .refused(let error) = self {
            return error
        }
        return nil
    }
}

/// What came of Reset Settings, read from `policy reset --json`. `not_invalid` means that the
/// settings file can be read, or that there is none, so there was nothing to set aside, which a
/// shell resetting takes as done (spec/CLI.md §7).
enum ResetOutcome: Equatable, Sendable {
    /// The invalid file was set aside as `policy.invalid.toml`, and the fresh policy put in its
    /// place.
    case reset
    case nothingToReset
    /// Anything else, as the verb ended.
    case failed(VerbOutcome)

    init(_ outcome: VerbOutcome) {
        switch outcome {
        case .done(let objects) where objects.contains(.policyReset):
            self = .reset
        case .refused(let error) where error.code == "not_invalid":
            self = .nothingToReset
        default:
            self = .failed(outcome)
        }
    }

    /// From the run itself. The host writes `policy_reset` only once the fresh policy is in place,
    /// so a run that wrote it reset the settings however it then ended: `HostCLI.run` looks for a
    /// Cancel before it looks for the exit, and a Cancel or the time limit can come just after the
    /// reset. So its output is read first, and only then how it ended.
    init(result: CLIResult) {
        if HostJSON.objects(inOutput: result.stdout).contains(.policyReset) {
            self = .reset
        } else {
            self = ResetOutcome(VerbOutcome(result))
        }
    }

    /// Whether the note offers Try Again: only when the host refused `policy_lock_failed`, which
    /// it does when the policy lock cannot be taken at all (the folder's file system refused it),
    /// never while another verb holds it: the reset then waits, up to the time limit, which gets
    /// no Try Again. Try Again runs the verb again, without asking again.
    var offersTryAgain: Bool {
        if case .failed(.refused(let error)) = self {
            return error.code == "policy_lock_failed"
        }
        return false
    }
}

enum HostJSON {
    /// A time as the host writes it; nil for anything else.
    static func time(_ text: String) -> Date? {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        return formatter.date(from: text)
    }

    /// The objects in a short verb's whole standard output, which is read whole, so no line bound
    /// applies (that bound is `run`'s). A line that cannot be used is dropped, and so is a last
    /// line with no newline.
    static func objects(inOutput text: String) -> [HostObject] {
        var lines = JSONLines(limit: nil)
        var items = lines.feed(Data(text.utf8))
        items += lines.finish()
        var found: [HostObject] = []
        for item in items {
            if case .object(let object) = item {
                found.append(object)
            }
        }
        return found
    }
}
