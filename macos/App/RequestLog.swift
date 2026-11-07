import Foundation

// Activity (decisions/0002 §15): what Lucille asked this Mac to do since the app opened, from
// `run`'s `request` events, kept in the app's memory and nowhere else, and said in words.
// Foundation only, so the self-test runs it without a screen.

/// One request, as far as `run` has told: when the app first saw it, what it asked for, and how
/// far it got.
struct RequestEntry: Equatable, Sendable {
    /// How far a request got.
    fileprivate enum Step: Equatable, Sendable {
        case received
        /// To run, and not started yet: an operation that starts no process goes from here to
        /// its end.
        case toRun
        /// Its process runs.
        case running
        case ended(RequestEnd)
        /// `run` stopped with no `finished` for it.
        case stoppedWithRun
    }

    /// Lucille's id for the job, which the app shows nowhere.
    let jobId: String
    /// The app's own clock when the first event of the request arrived: events carry no time.
    let firstSeen: Date
    /// The app's own clock when its latest event arrived, or when `run` ended with it open.
    fileprivate(set) var lastSeen: Date
    /// What the request asked for, as the host wrote it in `received`. This is Lucille's text: it
    /// is read only in this file, and shown only in the Activity panel, through `RequestText`.
    fileprivate var op: String?
    fileprivate var root: String?
    fileprivate var path: String?
    fileprivate var step: Step

    /// Whether `run` may still say more of it.
    fileprivate var isOpen: Bool {
        switch step {
        case .received, .toRun, .running:
            return true
        case .ended, .stoppedWithRun:
            return false
        }
    }

    /// Whether a `received` for its job id may be this request written again after a restart of
    /// `run`: while it is open, or once it stopped with `run`. A request that ended is never
    /// written again (spec/CLI.md §6).
    fileprivate var mayBeWrittenAgain: Bool {
        switch step {
        case .received, .toRun, .running, .stoppedWithRun:
            return true
        case .ended:
            return false
        }
    }
}

/// The requests `run` reported since the app opened, oldest first, one entry each, updated in
/// place as each goes on. At most `capacity` are kept; the one seen least recently goes first, and
/// one whose process runs goes last of all, so a burst of requests cannot hide that something
/// runs.
struct RequestLog: Equatable, Sendable {
    static let capacity = 500

    private(set) var entries: [RequestEntry] = []

    init() {}

    /// Whether an operation's process runs: some request has started and not finished.
    var operationRunning: Bool {
        entries.contains { $0.step == .running }
    }

    /// One `request` event, at the app's clock `now`. A request starts its entry at `received`,
    /// and one read before a restart of `run` and decided after it is written again from
    /// `received` (spec/CLI.md §6): its entry, still open or stopped with `run`, is updated in
    /// place, and keeps when it was first seen. A request that ended is never written again, so a
    /// `received` for its job id is a new request, with an entry of its own: the host forgets a
    /// job id 15 minutes after the request's `not_after`, or a day and 15 minutes after it arrived
    /// when it had none, and Lucille may then use it again. Every other event goes to the newest
    /// entry with its job id. An event for a request with no entry, or of a phase this release
    /// does not know, is ignored.
    mutating func note(_ event: RequestEvent, at now: Date) {
        let found = entries.lastIndex { $0.jobId == event.jobId }
        if case .received(let op, let root, let path) = event.phase {
            if let index = found, entries[index].mayBeWrittenAgain {
                entries[index].op = op
                entries[index].root = root
                entries[index].path = path
                entries[index].step = .received
                entries[index].lastSeen = now
            } else {
                entries.append(
                    RequestEntry(jobId: event.jobId, firstSeen: now, lastSeen: now, op: op, root: root, path: path, step: .received)
                )
                trim()
            }
            return
        }
        guard let index = found else {
            return
        }
        switch event.phase {
        case .received, .other:
            return
        case .decided(let run, let code):
            entries[index].step = run ? .toRun : .ended(.refused(code ?? ""))
        case .started:
            entries[index].step = .running
        case .finished(let end):
            entries[index].step = .ended(end)
        }
        entries[index].lastSeen = now
    }

    /// `run` has ended: a request it said no more of was stopped with it, and nothing runs.
    mutating func runEnded(at now: Date) {
        for index in entries.indices where entries[index].isOpen {
            entries[index].step = .stoppedWithRun
            entries[index].lastSeen = now
        }
    }

    private mutating func trim() {
        while entries.count > RequestLog.capacity {
            let waiting = entries.indices.filter { entries[$0].step != .running }
            let candidates = waiting.isEmpty ? Array(entries.indices) : waiting
            guard let oldest = candidates.min(by: { entries[$0].lastSeen < entries[$1].lastSeen }) else {
                return
            }
            entries.remove(at: oldest)
        }
    }
}

/// The only place Lucille's text becomes words: what a request asked for, quoted or not shown at
/// all, and how it ended. No sentence says that Lucille received anything: `finished` says the
/// request finished on this Mac, not that Lucille got the reply (spec/CLI.md §6).
enum RequestText {
    private static let appName = HostText.appName

    /// The longest path shown, in characters, its quotes left out; a longer one is cut in the
    /// middle.
    static let longestPath = 60
    /// The longest line of the host's log shown, in characters.
    static let longestLogLine = 2000

    static let running = "Running…"
    static let finished = "Finished on this Mac."
    static let abandoned = "Dropped: the connection changed."
    static let stoppedWithRun = "Stopped when \(appName)'s helper program stopped."
    static let unreadableRequest = "Lucille sent a request this Mac couldn't read."
    static let unshownPath = "a path that can't be shown"
    /// A refusal or failure whose code this release does not know: the codes are closed within
    /// the protocol's version 1, so this is never expected.
    static let otherRefusal = "Turned away by this Mac."
    static let otherFailure = "It didn't finish on this Mac."

    /// Each of the protocol's refusal codes (spec/PROTOCOL.md §11), as a fact. A refusal for an
    /// ability that is off never suggests turning it on: a compromised Lucille must not be able to
    /// nudge the user through the app (decisions/0002 §13).
    static let refused: [String: String] = [
        "unknown_op": "Turned away: this \(appName) has no such ability.",
        "not_enabled": "Turned away: that ability is off on this Mac.",
        "bad_request": "Turned away: the request wasn't written the way this Mac accepts.",
        "bad_args": "Turned away: what it asked for doesn't fit that ability.",
        "path_outside_root": "Turned away: this Mac doesn't let Lucille reach that path.",
        "expired": "Turned away: the request was out of date.",
        "paused": "Turned away: Lucille was paused on this Mac.",
        "host_too_old": "Turned away: Lucille asked for a newer \(appName).",
    ]

    /// Each code of a request that was to run and did not finish.
    static let failed: [String: String] = [
        "timeout": "Stopped: it ran past its time limit.",
        "killed_locally": "Stopped on this Mac.",
        "interrupted": "Interrupted: it may or may not have run.",
    ]

    /// What the request asked for, in words: the core's summary of the operation, from
    /// `ops list --json` (`summaries`, by name), first letter in lower case; then its path, in
    /// quotes, and its folder's alias. An operation the summaries lack is named, in quotes.
    static func line(for entry: RequestEntry, summaries: [String: String]) -> String {
        guard let op = entry.op, let named = quoted(op) else {
            return unreadableRequest
        }
        var words: String
        if let summary = summaries[op], let first = summary.first {
            words = "Lucille asked to " + first.lowercased() + String(summary.dropFirst())
        } else {
            words = "Lucille asked for " + named
        }
        if let path = entry.path {
            words += ": " + (quoted(path) ?? unshownPath)
        } else if entry.root != nil {
            // The host names the folder and leaves out the path when the path started with `/`
            // or held a character it does not show (spec/CLI.md §6).
            words += ": " + unshownPath
        }
        if let root = entry.root, Alias.isValid(root) {
            words += " in " + root
        }
        return words + "."
    }

    /// How the request ended, or that it runs; nil while it waits to be decided or to start.
    static func outcome(for entry: RequestEntry) -> String? {
        switch entry.step {
        case .received, .toRun:
            return nil
        case .running:
            return running
        case .ended(.done):
            return finished
        case .ended(.refused(let code)):
            return refused[code] ?? otherRefusal
        case .ended(.failed(let code)):
            return failed[code] ?? otherFailure
        case .ended(.abandoned):
            return abandoned
        case .stoppedWithRun:
            return stoppedWithRun
        }
    }

    /// Lucille's text in quotes, cut in the middle to `longestPath` characters; nil when it holds
    /// a character that could make it read as something else: a control, format (a reordering
    /// mark or a zero-width one), line or paragraph separator character, or a quotation mark that
    /// opens or closes (a curly quote or apostrophe, a guillemet), which could end the app's own
    /// quotes and go on in its words. The host writes as null a path that holds a control,
    /// reordering or separator character (spec/CLI.md §6), and passes the other format
    /// characters, such as a zero-width space, and every quotation mark as they are: for those,
    /// this check is the only one.
    static func quoted(_ text: String) -> String? {
        if text.isEmpty {
            return nil
        }
        for scalar in text.unicodeScalars {
            switch scalar.properties.generalCategory {
            case .control, .format, .lineSeparator, .paragraphSeparator, .initialPunctuation, .finalPunctuation:
                return nil
            default:
                continue
            }
        }
        guard text.count > longestPath else {
            return "\u{201C}" + text + "\u{201D}"
        }
        let kept = longestPath - 1
        let head = String(text.prefix(kept - kept / 2))
        let tail = String(text.suffix(kept / 2))
        return "\u{201C}" + head + "\u{2026}" + tail + "\u{201D}"
    }

    /// One line of the host's log, as the host wrote it, cut to `longestLogLine` characters.
    static func logLine(_ text: String) -> String {
        guard text.count > longestLogLine else {
            return text
        }
        return String(text.prefix(longestLogLine - 1)) + "\u{2026}"
    }
}
