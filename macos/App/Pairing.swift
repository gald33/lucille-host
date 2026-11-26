import Foundation

/// The numbers and rules the pairing flow works by.
enum Pairing {
    /// How often `pair status` asks while the window shows the waiting page.
    static let pollInterval: TimeInterval = 5
    /// How long after the SAS deadline the app asks `pair status`: the deadline is written to the
    /// second, so asking at once could find the SAS still pending while no screen shows it.
    static let deadlineGrace: TimeInterval = 3
    /// How long after asking past the deadline the app asks again, if the SAS was still pending.
    static let recheckDelay: TimeInterval = 5
    /// How long after the number is drawn a click on They Match first counts: a click queued
    /// before the number was on the screen is not an answer to it (decisions/0001 "Pairing").
    static let matchDelay: TimeInterval = 0.3
    /// `pair confirm` refusals that mean the SAS is not on this screen yet: the app asks
    /// `pair status` again, and draws what it returns.
    static let redrawCodes: Set<String> = ["waiting", "sas_ready"]

    /// One pair's SAS, by the pair's name and its deadline.
    static func key(name: String, deadline: Date) -> String {
        "\(name)|\(Int(deadline.timeIntervalSince1970))"
    }
}

/// The rules of Connect from Lucille's chat (decisions/0002 §11.4 and §11.5): the services, the
/// user's username as the host normalizes it, and the forms of what `connect` objects carry. The
/// host checks everything again; these only keep the window from offering what it would refuse,
/// and from showing what is not in the host's form.
enum Connect {
    /// The chat services the app connects through, as the host names them (`connect.SERVICES`).
    static let services: [String] = ["telegram"]
    /// How long the `pair begin` or `pair resume` child may run before it is interrupted, which
    /// keeps the request: the request's 20 minutes, its 300 s of skew, and the wait for the SAS
    /// after Lucille's answer.
    static let childTimeLimit: TimeInterval = 1800

    /// A service's name, as the window shows it.
    static func serviceName(_ service: String) -> String {
        switch service {
        case "telegram":
            return "Telegram"
        default:
            return service
        }
    }

    private static let crockford = Set("0123456789ABCDEFGHJKMNPQRSTVWXYZ".unicodeScalars)

    /// Whether `text` is a connect code as the host writes one: two groups of five symbols of
    /// Crockford's base32, upper case, joined by `-`.
    static func isCode(_ text: String) -> Bool {
        let scalars = Array(text.unicodeScalars)
        guard scalars.count == 11 else {
            return false
        }
        for (index, scalar) in scalars.enumerated() {
            if index == 5 {
                if scalar != "-" {
                    return false
                }
            } else if !crockford.contains(scalar) {
                return false
            }
        }
        return true
    }

    /// Whether `text` is a username as the host normalizes one: a lower-case ASCII letter, then
    /// up to 63 of `a-z`, `0-9` and `_`.
    static func isUsername(_ text: String) -> Bool {
        let scalars = Array(text.unicodeScalars)
        guard (1...64).contains(scalars.count), ("a"..."z").contains(scalars[0]) else {
            return false
        }
        return scalars.allSatisfy { ("a"..."z").contains($0) || ("0"..."9").contains($0) || $0 == "_" }
    }

    /// Whether `text` is a signed handle in its form: `@`, an ASCII letter, then up to 63 ASCII
    /// letters, digits and `_`.
    static func isHandle(_ text: String) -> Bool {
        let scalars = Array(text.unicodeScalars)
        guard (2...65).contains(scalars.count), scalars[0] == "@" else {
            return false
        }
        let letter: (Unicode.Scalar) -> Bool = { ("a"..."z").contains($0) || ("A"..."Z").contains($0) }
        guard letter(scalars[1]) else {
            return false
        }
        return scalars.dropFirst(2).allSatisfy { letter($0) || ("0"..."9").contains($0) || $0 == "_" }
    }

    /// The username a typed text names on `service`, as the host's `normalize_username` gives it,
    /// or nil if it names none. In this order: refuse any character outside ASCII; drop the spaces
    /// (U+0020) around it; drop a leading `<service>:`, matched without regard to ASCII case,
    /// which must name `service`; drop one leading `@`; lower ASCII letters only.
    static func normalizedUsername(_ text: String, service: String) -> String? {
        guard services.contains(service), text.unicodeScalars.allSatisfy(\.isASCII) else {
            return nil
        }
        var scalars = Substring(text).unicodeScalars[...]
        while scalars.first == " " {
            scalars = scalars.dropFirst()
        }
        while scalars.last == " " {
            scalars = scalars.dropLast()
        }
        var name = String(String.UnicodeScalarView(scalars))
        if let colon = name.firstIndex(of: ":") {
            guard asciiLower(String(name[..<colon])) == service else {
                return nil
            }
            name = String(name[name.index(after: colon)...])
        }
        if name.hasPrefix("@") {
            name.removeFirst()
        }
        let lowered = asciiLower(name)
        return isUsername(lowered) ? lowered : nil
    }

    /// `text` with ASCII letters lowered and nothing else changed: never a Unicode lowering.
    private static func asciiLower(_ text: String) -> String {
        var kept = String.UnicodeScalarView()
        for scalar in text.unicodeScalars {
            if ("A"..."Z").contains(scalar), let lower = Unicode.Scalar(scalar.value + 32) {
                kept.append(lower)
            } else {
                kept.append(scalar)
            }
        }
        return String(kept)
    }

    /// The hint the account page shows under what was typed; nil while it is empty or names a
    /// username.
    static func hint(for typed: String, service: String) -> String? {
        if typed.isEmpty || normalizedUsername(typed, service: service) != nil {
            return nil
        }
        return ConnectionText.usernameHint(service: service)
    }

    /// The codes that end a request, which is then spent (spec/CLI.md §7, `pair begin`): the note
    /// says why and offers Start Over.
    static let endingCodes: Set<String> = [
        "interference", "connect_expired", "newer_capsule", "key_misused", "other_account", "not_this_mac",
    ]
    /// The refusals of the code Lucille delivered, which end a request too.
    static let deliveredCodeRefusals: Set<String> = ["bad_code", "wrong_hub", "code_has_token"]
    /// Refusals of `pair begin` or `pair resume` after which the account page comes back, to
    /// begin again: nothing was begun, or the hub could not be reached and no request was kept.
    static let accountAgainCodes: Set<String> = ["bad_service", "bad_username", "hub_error", "busy", "lock_failed"]
    /// Refusals that leave a request the host kept as it was.
    static let keepsRequestCodes: Set<String> = ["interrupted", "busy", "lock_failed", "held"]

    /// What `pair begin` reads on standard input: the service, then the username, one per line
    /// (`HostChild` adds the last newline).
    static func beginInput(service: String, username: String) -> String {
        "\(service)\n\(username)"
    }
}

/// A note the pairing flow asks the app to show. `ConnectionText.note(_:)` gives its words.
enum PairingNote: Equatable, Sendable {
    /// The host refused: its code, its sentence, and whether a SAS was shown in this flow, which
    /// decides what `expired` means.
    case refused(code: String, message: String?, afterSAS: Bool)
    case confirmed
    /// The pair confirmed on this Mac is not the one the window was setting up: `pair confirm`
    /// confirmed another, or `status` found another confirmed.
    case confirmedAnother
    /// A confirm that ended with no answer, after which `status` says it was not confirmed.
    case notConfirmed
    /// A confirm that ended with no answer, and `status` could not say either.
    case confirmUnknown
    case rejected(lucilleTold: Bool, afterDiffer: Bool)
    /// `pair reject` refused, or ended with no answer.
    case rejectFailed(HostError?)
    /// The pairing child could not be started.
    case notStarted
    /// A Connect request ended (spec/CLI.md §7, `pair begin`): its code and the host's sentence.
    /// The note offers Start Over.
    case connectEnded(code: String, message: String?)
    /// `pair cancel` refused, or ended with no answer: the request may still be kept.
    case cancelFailed(HostError?)
}

/// The pairing flow, decisions/0001's gate: the Mac shows the SAS from `sas_pending`, and the user
/// clicks They Match only if Lucille's number in the chat is the same (spec/CLI.md §2 and §7). The
/// pairing comes from Connect from Lucille's chat (decisions/0002 §11.4): `pair begin`, then
/// `pair resume` after a relaunch or an interruption, and `pair cancel` to stop. The app no longer
/// pastes a code (`pair --code-stdin`). It is a value: inputs in, effects out. It draws nothing and starts nothing; the app's glue runs each
/// effect and feeds back what it did, with the `epoch` the effect was issued in, and the flow
/// drops anything from an older epoch.
///
/// The rules it keeps, each a self-test check:
/// - `pair confirm` runs only after They Match, clicked on the drawn number, and after a `status`
///   read right before it reports the same pair, with the same deadline, still pending: once for
///   each They Match.
/// - Each `sas_pending` must be drawn again before They Match counts.
/// - `pair status` runs only while the window shows the waiting or the compare page.
/// - After They Differ, They Match is never enabled again for that pair and deadline.
/// - Connect to Lucille… reads `pair status` first: a request the host kept is taken up with
///   `pair resume`, never begun again; with none, the account page asks for the service and the
///   username before any code is shown.
/// - `pair begin` starts only with a username in the host's form, and `pair cancel` runs only once
///   no `pair begin` or `pair resume` child lives.
/// - A request that ended says why, and never names the account Lucille signed for another.
struct PairingFlow: Equatable, Sendable {
    /// Why the flow reads `status --json`.
    enum StatusPurpose: Equatable, Sendable {
        /// Right before `pair confirm`: the SAS shown must still be the one pending.
        case beforeConfirm
        /// After a confirm that ended with no answer: was it confirmed?
        case afterConfirm
        /// After the pairing child ended with no answer: where is the pair?
        case afterChild
    }

    enum Verb: Equatable, Sendable {
        case pairStatus
        case pairConfirm
        case pairReject
        case pairCancel
        case status(StatusPurpose)
    }

    enum Busy: Equatable, Sendable {
        case checking
        case confirming
        case rejecting
        /// `pair cancel` runs, or waits for the `pair begin` or `pair resume` child to end.
        case cancelling
    }

    /// The number on the compare page.
    struct Comparison: Equatable, Sendable {
        let name: String
        let sas: String
        let deadline: Date
        /// False for a pair the user said differs: They Match stays off.
        let canMatch: Bool
        /// When the number was drawn on a visible window, on the clock NSEvent timestamps use
        /// (seconds since the Mac started, sleep excluded); nil until then.
        var drawnAt: TimeInterval?

        var drawn: Bool { drawnAt != nil }
    }

    enum Screen: Equatable, Sendable {
        case closed
        /// The chat service and the user's own username there (decisions/0002 §11.5).
        case account
        /// `pair begin` runs, and has not yet written its code.
        case starting
        /// The connect code to send Lucille, while the request is live.
        case connectCode(ConnectRequest)
        case waiting(clockSkewed: Bool)
        case compare(Comparison)
        case busy(Busy)
    }

    enum Effect: Equatable, Sendable {
        /// Start `pair begin --json` with the service and the username on its standard input.
        case startConnect(service: String, username: String)
        /// Start `pair resume --json`, on the request the host kept.
        case resumeConnect
        /// SIGINT the pairing child, which keeps the pair.
        case interruptPair
        case run(Verb)
        /// Send `pollDue` after this many seconds.
        case pollAfter(TimeInterval)
        case cancelPoll
        /// Send `deadlinePassed` at this wall-clock time.
        case deadlineAt(Date)
        case cancelDeadline
        case say(PairingNote)
        /// Read `status --json` again for the menu. It starts nothing.
        case refreshStatus
        /// Read `status --json` and hand it to the run supervisor, which starts `run` if the pair
        /// is confirmed.
        case startRunCheck
        /// Tell the run supervisor to drop its pending restarts: a pairing started or stopped.
        case cancelRunRetries
    }

    enum Input: Equatable, Sendable {
        /// The menu's Connect to Lucille…: the code window.
        case startConnecting
        /// A relaunch, or the menu's Finish Connecting…: where `status` says the pair is.
        case resume(StatusPair)
        /// Continue on the account page, with the service chosen and the username typed.
        case submitAccount(service: String, typed: String)
        case childNotStarted
        case childObject(HostObject)
        case childExited(ChildEnd)
        case verbResult(Verb, VerbOutcome)
        /// The number is drawn on a visible, uncovered window, at this time (`Comparison.drawnAt`).
        case sasDrawn(String, at: TimeInterval)
        /// They Match, with the click's event time, on the same clock.
        case theyMatch(at: TimeInterval)
        case theyDiffer
        case pollDue
        /// The deadline timer fired, at this wall-clock time.
        case deadlinePassed(at: Date)
        /// The user closed the window.
        case windowClosed
        /// Stop Connecting, from the menu or the waiting page, once the user has said so.
        case stopConnecting
        /// The pairing child has run for `Pairing.childTimeLimit`.
        case childTimeLimit
    }

    private(set) var screen: Screen = .closed
    private(set) var epoch = 0
    private(set) var pairChildAlive = false
    /// A SAS was shown in this flow: it decides which `expired` sentence the user reads.
    private(set) var shownSAS = false
    private(set) var name: String?
    private var lastError: HostError?
    private var target: Comparison?
    private var rejectWhenChildEnds = false
    private var rejectAfterDiffer = false
    private var lastDeadlineCheck: Date?
    /// Which `status` read the checking page waits for: before a confirm, or after one that ended
    /// with no answer. Each is answered once; any other answer is dropped.
    private var checking: StatusPurpose?
    /// The pairs, by name and deadline, the user said differ.
    private var latched: Set<String> = []
    /// The Connect request the host last reported, and whether it keeps one, as far as this run
    /// of the app has seen: the menu then offers Finish Connecting… and Stop Connecting….
    private(set) var request: ConnectRequest?
    private(set) var requestKept = false
    /// The account Lucille signed, once `connecting` has said it: the window shows it.
    private(set) var signedAccount: SignedAccount?
    /// The `pair status` read that Connect to Lucille… makes first is out.
    private var lookingForRequest = false
    private var cancelWhenChildEnds = false

    init() {}

    var isBusy: Bool {
        if case .busy = screen {
            return true
        }
        return false
    }

    /// Whether the pairing window is open.
    var windowOpen: Bool { screen != .closed }

    mutating func handle(_ input: Input, epoch inputEpoch: Int) -> [Effect] {
        if case .childExited(let end) = input {
            return childExited(end, current: inputEpoch == epoch)
        }
        if case .childNotStarted = input {
            pairChildAlive = false
            rejectWhenChildEnds = false
            cancelWhenChildEnds = false
            guard inputEpoch == epoch else {
                return []
            }
            screen = .closed
            return [.say(.notStarted), .refreshStatus]
        }
        guard inputEpoch == epoch else {
            return []
        }
        switch input {
        case .startConnecting:
            return startConnecting()
        case .resume(let pair):
            return resume(pair)
        case .submitAccount(let service, let typed):
            return submitAccount(service: service, typed: typed)
        case .childObject(let object):
            return childObject(object)
        case .verbResult(let verb, let outcome):
            return verbResult(verb, outcome)
        case .sasDrawn(let sas, let at):
            return sasDrawn(sas, at: at)
        case .theyMatch(let at):
            return theyMatch(at: at)
        case .theyDiffer:
            return theyDiffer()
        case .pollDue:
            return pollDue()
        case .deadlinePassed(let at):
            return deadlinePassed(at: at)
        case .windowClosed:
            return windowClosed()
        case .stopConnecting:
            return stopConnecting()
        case .childTimeLimit:
            return pairChildAlive ? [.interruptPair] : []
        case .childExited, .childNotStarted:
            return []
        }
    }

    // MARK: Starting

    private mutating func beginFlow() {
        name = nil
        lastError = nil
        target = nil
        shownSAS = false
        lastDeadlineCheck = nil
        checking = nil
        signedAccount = nil
        lookingForRequest = false
        cancelWhenChildEnds = false
    }

    /// Connect to Lucille…, or Finish Connecting… for a request: `pair status` first, so that a
    /// request the host kept, from before a relaunch too, is taken up and not begun again.
    private mutating func startConnecting() -> [Effect] {
        guard !pairChildAlive, !isBusy else {
            return []
        }
        epoch += 1
        beginFlow()
        lookingForRequest = true
        screen = .busy(.checking)
        return [.cancelRunRetries, .cancelPoll, .cancelDeadline, .run(.pairStatus)]
    }

    /// What the first `pair status` found: a request to take up, a pair to go on with, or
    /// nothing, and then the account page.
    private mutating func lookedForRequest(_ outcome: VerbOutcome) -> [Effect] {
        switch outcome {
        case .done:
            if let found = outcome.connectState {
                switch found {
                case .code(let kept):
                    request = kept
                    requestKept = true
                    return takeUp(on: .connectCode(kept))
                case .delivered:
                    requestKept = true
                    return takeUp(on: .waiting(clockSkewed: false))
                case .connecting, .cancelled, .other:
                    screen = .account
                    return []
                }
            }
            requestKept = false
            switch outcome.pair {
            case .waiting(let pairName)?:
                name = pairName
                screen = .waiting(clockSkewed: false)
                return pollAgain()
            case .sasPending(let pairName, let sas, let deadline)?:
                return showSAS(name: pairName, sas: sas, deadline: deadline)
            case .confirmed?:
                screen = .closed
                return [.startRunCheck]
            default:
                screen = .account
                return []
            }
        case .refused(let error):
            return refused(error)
        case .cancelled:
            screen = .closed
            return [.refreshStatus]
        default:
            // `pair status` could not say: `pair begin` replaces any request the host kept.
            screen = .account
            return []
        }
    }

    /// `pair resume`, on the request the host kept, with `page` on the screen meanwhile.
    private mutating func takeUp(on page: Screen) -> [Effect] {
        screen = page
        pairChildAlive = true
        lastError = nil
        shownSAS = false
        return [.cancelPoll, .resumeConnect]
    }

    private mutating func submitAccount(service: String, typed: String) -> [Effect] {
        guard screen == .account, !pairChildAlive,
              let username = Connect.normalizedUsername(typed, service: service)
        else {
            return []
        }
        screen = .starting
        pairChildAlive = true
        lastError = nil
        shownSAS = false
        request = nil
        signedAccount = nil
        return [.startConnect(service: service, username: username)]
    }

    private mutating func resume(_ pair: StatusPair) -> [Effect] {
        guard !pairChildAlive, !isBusy else {
            return []
        }
        switch pair {
        case .waiting(let pairName), .sasReady(let pairName):
            epoch += 1
            beginFlow()
            name = pairName
            screen = .waiting(clockSkewed: false)
            return [.cancelRunRetries, .cancelPoll, .cancelDeadline, .run(.pairStatus)]
        case .sasPending(let pairName, _):
            epoch += 1
            beginFlow()
            name = pairName
            shownSAS = true
            screen = .waiting(clockSkewed: false)
            return [.cancelRunRetries, .cancelPoll, .cancelDeadline, .run(.pairStatus)]
        case .expired:
            return [.say(.refused(code: "expired", message: nil, afterSAS: true)), .refreshStatus]
        case .refused:
            return [.say(.refused(code: "no_gate", message: nil, afterSAS: false))]
        case .noPair, .confirmed, .severalPairs, .unreadable, .other:
            return []
        }
    }

    // MARK: The pairing child

    private mutating func childObject(_ object: HostObject) -> [Effect] {
        switch object {
        case .connectState(.code(let live)):
            request = live
            requestKept = true
            switch screen {
            case .starting, .connectCode, .waiting:
                screen = .connectCode(live)
            default:
                break
            }
            return []
        case .connectState(.connecting(let account)):
            // Lucille's answer passed, for the account typed: the host answers its code next, and
            // deletes the request once the answer is tried.
            signedAccount = account
            requestKept = false
            switch screen {
            case .starting, .connectCode, .waiting:
                screen = .waiting(clockSkewed: false)
            default:
                break
            }
            return []
        case .pairCode(let pairName):
            name = pairName
            return []
        case .pair(.accepted(let pairName, let skewed)):
            name = pairName
            if case .waiting = screen {
                screen = .waiting(clockSkewed: skewed)
            }
            return []
        case .pair(.waiting(let pairName)):
            name = pairName
            return []
        case .pair(.sasPending(let pairName, let sas, let deadline)):
            return showSAS(name: pairName, sas: sas, deadline: deadline)
        case .error(let error):
            lastError = error
            return []
        case .otherVersion:
            lastError = HostError(code: "internal", message: "the helper program wrote output of another version", kind: nil)
            return pairChildAlive ? [.interruptPair] : []
        default:
            return []
        }
    }

    private mutating func childExited(_ end: ChildEnd, current: Bool) -> [Effect] {
        pairChildAlive = false
        let error = lastError
        lastError = nil
        if cancelWhenChildEnds {
            cancelWhenChildEnds = false
            rejectWhenChildEnds = false
            if signedAccount == nil && name == nil {
                return [.run(.pairCancel)]
            }
            // The answer was tried before the child ended: there is a pair now, to turn down.
            screen = .busy(.rejecting)
            return [.run(.pairReject)]
        }
        if rejectWhenChildEnds {
            rejectWhenChildEnds = false
            return [.run(.pairReject)]
        }
        guard current else {
            return []
        }
        if case .exited(let status) = end, status == 1 || status == 2, let error {
            if error.code == "interrupted" {
                // The pair, or the request, is kept. While the window is open, `pair status` goes
                // on asking, and takes a request up again with `pair resume`.
                switch screen {
                case .waiting, .connectCode, .starting:
                    return [.run(.pairStatus)]
                default:
                    return [.refreshStatus]
                }
            }
            return refused(error)
        }
        if case .exited(0) = end, shownSAS {
            return []
        }
        // A signal, or an exit with no `error` object and no SAS: learn where the pair is from
        // `status` (spec/CLI.md §4.3).
        return [.run(.status(.afterChild))]
    }

    // MARK: Verb results

    private mutating func verbResult(_ verb: Verb, _ outcome: VerbOutcome) -> [Effect] {
        switch verb {
        case .pairStatus:
            return pairStatusResult(outcome)
        case .pairConfirm:
            return confirmResult(outcome)
        case .pairReject:
            return rejectResult(outcome)
        case .pairCancel:
            return cancelResult(outcome)
        case .status(.beforeConfirm):
            return beforeConfirm(outcome)
        case .status(.afterConfirm):
            return afterConfirm(outcome)
        case .status(.afterChild):
            return afterChild(outcome)
        }
    }

    private mutating func pairStatusResult(_ outcome: VerbOutcome) -> [Effect] {
        if lookingForRequest {
            lookingForRequest = false
            guard case .busy(.checking) = screen else {
                return []
            }
            return lookedForRequest(outcome)
        }
        if isBusy {
            return []
        }
        switch outcome {
        case .done:
            if let found = outcome.connectState {
                return requestStatus(found)
            }
            guard let state = outcome.pair else {
                return pollAgain()
            }
            switch state {
            case .sasPending(let pairName, let sas, let deadline):
                return showSAS(name: pairName, sas: sas, deadline: deadline)
            case .waiting(let pairName):
                name = pairName
                return pollAgain()
            case .confirmed:
                screen = .closed
                target = nil
                return [.cancelPoll, .cancelDeadline, .startRunCheck]
            case .noPair:
                return closeFor(.noPair)
            case .accepted, .rejected, .other:
                return pollAgain()
            }
        case .refused(let error):
            if Pairing.redrawCodes.contains(error.code) {
                return pollAgain()
            }
            return refused(error)
        default:
            return pollAgain()
        }
    }

    /// `pair status` reported the request, while the window waits with no child: take it up.
    private mutating func requestStatus(_ found: ConnectState) -> [Effect] {
        guard !pairChildAlive else {
            return []
        }
        switch screen {
        case .connectCode, .waiting, .starting:
            break
        default:
            return []
        }
        switch found {
        case .code(let kept):
            request = kept
            requestKept = true
            return takeUp(on: .connectCode(kept))
        case .delivered:
            requestKept = true
            return takeUp(on: .waiting(clockSkewed: false))
        case .connecting, .cancelled, .other:
            return pollAgain()
        }
    }

    private mutating func cancelResult(_ outcome: VerbOutcome) -> [Effect] {
        guard case .busy(.cancelling) = screen else {
            return []
        }
        screen = .closed
        switch outcome {
        case .done:
            // A cancel says nothing more (decisions/0002 §11.4, step 5).
            requestKept = false
            request = nil
            return [.refreshStatus]
        case .refused(let error):
            if error.code == "none" {
                requestKept = false
                request = nil
                return [.refreshStatus]
            }
            return [.say(.cancelFailed(error)), .refreshStatus]
        default:
            return [.say(.cancelFailed(nil)), .refreshStatus]
        }
    }

    private mutating func beforeConfirm(_ outcome: VerbOutcome) -> [Effect] {
        guard case .busy(.checking) = screen, checking == .beforeConfirm, let shown = target else {
            return []
        }
        checking = nil
        guard let status = outcome.status else {
            // `status` could not say: confirm nothing, and draw the number again.
            target = nil
            screen = .waiting(clockSkewed: false)
            return [.run(.pairStatus)]
        }
        switch status.pair {
        case .sasPending(let pairName, let deadline) where pairName == shown.name && deadline == shown.deadline:
            screen = .busy(.confirming)
            return [.run(.pairConfirm)]
        case .confirmed(let pairName):
            screen = .closed
            target = nil
            if pairName == shown.name {
                return [.startRunCheck]
            }
            return [.say(.confirmedAnother), .refreshStatus]
        case .waiting, .sasReady, .sasPending:
            // Another pair, another deadline, or a SAS no command has shown: draw it again first.
            target = nil
            screen = .waiting(clockSkewed: false)
            return [.run(.pairStatus)]
        default:
            return closeFor(status.pair)
        }
    }

    private mutating func confirmResult(_ outcome: VerbOutcome) -> [Effect] {
        guard case .busy(.confirming) = screen, let shown = target else {
            return []
        }
        switch outcome {
        case .done:
            if case .confirmed(let pairName)? = outcome.pair {
                screen = .closed
                target = nil
                if pairName == shown.name {
                    return [.say(.confirmed), .startRunCheck]
                }
                return [.say(.confirmedAnother), .refreshStatus]
            }
        case .refused(let error):
            target = nil
            if Pairing.redrawCodes.contains(error.code) {
                screen = .waiting(clockSkewed: false)
                return [.run(.pairStatus)]
            }
            return refused(error)
        default:
            break
        }
        // No answer (a time limit, Cancel, a signal): ask `status` whether it was confirmed.
        screen = .busy(.checking)
        checking = .afterConfirm
        return [.run(.status(.afterConfirm))]
    }

    private mutating func afterConfirm(_ outcome: VerbOutcome) -> [Effect] {
        guard case .busy(.checking) = screen, checking == .afterConfirm, let shown = target else {
            return []
        }
        checking = nil
        screen = .closed
        target = nil
        guard let status = outcome.status else {
            return [.say(.confirmUnknown), .refreshStatus]
        }
        if case .confirmed(let pairName) = status.pair {
            if pairName == shown.name {
                return [.say(.confirmed), .startRunCheck]
            }
            return [.say(.confirmedAnother), .refreshStatus]
        }
        return [.say(.notConfirmed), .refreshStatus]
    }

    private mutating func afterChild(_ outcome: VerbOutcome) -> [Effect] {
        guard case .waiting = screen else {
            return [.refreshStatus]
        }
        guard let status = outcome.status else {
            screen = .closed
            return [.refreshStatus]
        }
        switch status.pair {
        case .waiting, .sasReady, .sasPending:
            return [.run(.pairStatus)]
        case .confirmed(let pairName):
            screen = .closed
            if let known = name, known != pairName {
                // Not the pair this window was setting up: say so, and start nothing.
                return [.say(.confirmedAnother), .refreshStatus]
            }
            return [.startRunCheck]
        default:
            return closeFor(status.pair)
        }
    }

    private mutating func rejectResult(_ outcome: VerbOutcome) -> [Effect] {
        guard case .busy(.rejecting) = screen else {
            return []
        }
        screen = .closed
        let differ = rejectAfterDiffer
        rejectAfterDiffer = false
        switch outcome {
        case .done:
            if case .rejected(_, let told)? = outcome.pair {
                return [.say(.rejected(lucilleTold: told, afterDiffer: differ)), .refreshStatus]
            }
            return [.say(.rejectFailed(nil)), .refreshStatus]
        case .refused(let error):
            if error.code == "none" {
                return [.say(.refused(code: "none", message: error.message, afterSAS: shownSAS)), .refreshStatus]
            }
            return [.say(.rejectFailed(error)), .refreshStatus]
        default:
            return [.say(.rejectFailed(nil)), .refreshStatus]
        }
    }

    // MARK: The compare page

    private mutating func showSAS(name pairName: String, sas: String, deadline: Date) -> [Effect] {
        name = pairName
        shownSAS = true
        if isBusy {
            return []
        }
        let latchedKey = Pairing.key(name: pairName, deadline: deadline)
        screen = .compare(
            Comparison(name: pairName, sas: sas, deadline: deadline, canMatch: !latched.contains(latchedKey), drawnAt: nil)
        )
        var wake = deadline.addingTimeInterval(Pairing.deadlineGrace)
        if let checked = lastDeadlineCheck, wake <= checked {
            wake = checked.addingTimeInterval(Pairing.recheckDelay)
        }
        return [.cancelPoll, .deadlineAt(wake)]
    }

    private mutating func sasDrawn(_ sas: String, at: TimeInterval) -> [Effect] {
        guard case .compare(var shown) = screen, shown.sas == sas, shown.drawnAt == nil else {
            return []
        }
        shown.drawnAt = at
        screen = .compare(shown)
        return []
    }

    private mutating func theyMatch(at: TimeInterval) -> [Effect] {
        guard case .compare(let shown) = screen, shown.canMatch, let drawnAt = shown.drawnAt,
              at >= drawnAt + Pairing.matchDelay
        else {
            return []
        }
        target = shown
        screen = .busy(.checking)
        checking = .beforeConfirm
        return [.cancelDeadline, .run(.status(.beforeConfirm))]
    }

    private mutating func theyDiffer() -> [Effect] {
        guard case .compare(let shown) = screen else {
            return []
        }
        latched.insert(Pairing.key(name: shown.name, deadline: shown.deadline))
        epoch += 1
        target = nil
        screen = .busy(.rejecting)
        rejectAfterDiffer = true
        return [.cancelRunRetries, .cancelPoll, .cancelDeadline, .run(.pairReject)]
    }

    /// Whether Stop Connecting stops a Connect request, not a pair: no answer has been tried yet.
    private var stopsRequest: Bool {
        guard signedAccount == nil, name == nil else {
            return false
        }
        switch screen {
        case .starting, .connectCode:
            return true
        case .waiting:
            return pairChildAlive || requestKept
        case .closed:
            return requestKept
        default:
            return false
        }
    }

    private mutating func stopConnecting() -> [Effect] {
        if isBusy {
            return []
        }
        if stopsRequest {
            epoch += 1
            target = nil
            screen = .busy(.cancelling)
            var effects: [Effect] = [.cancelRunRetries, .cancelPoll, .cancelDeadline]
            if pairChildAlive {
                // `pair cancel` is `busy` while the child waits on the request: stop it first.
                cancelWhenChildEnds = true
                effects.append(.interruptPair)
            } else {
                effects.append(.run(.pairCancel))
            }
            return effects
        }
        if case .compare(let shown) = screen {
            latched.insert(Pairing.key(name: shown.name, deadline: shown.deadline))
        }
        epoch += 1
        target = nil
        screen = .busy(.rejecting)
        rejectAfterDiffer = false
        var effects: [Effect] = [.cancelRunRetries, .cancelPoll, .cancelDeadline]
        if pairChildAlive {
            rejectWhenChildEnds = true
            effects.append(.interruptPair)
        } else {
            effects.append(.run(.pairReject))
        }
        return effects
    }

    // MARK: Timers and the window

    private func pollAgain() -> [Effect] {
        switch screen {
        case .waiting, .connectCode:
            return pairChildAlive ? [] : [.pollAfter(Pairing.pollInterval)]
        default:
            return []
        }
    }

    private func pollDue() -> [Effect] {
        guard !pairChildAlive else {
            return []
        }
        switch screen {
        case .waiting, .connectCode:
            return [.run(.pairStatus)]
        default:
            return []
        }
    }

    private mutating func deadlinePassed(at: Date) -> [Effect] {
        guard case .compare = screen else {
            return []
        }
        lastDeadlineCheck = at
        return [.run(.pairStatus)]
    }

    private mutating func windowClosed() -> [Effect] {
        switch screen {
        case .account, .starting, .connectCode, .waiting, .compare:
            screen = .closed
            var effects: [Effect] = [.cancelPoll, .cancelDeadline]
            if pairChildAlive {
                effects.append(.interruptPair)
            }
            return effects
        case .closed, .busy:
            return []
        }
    }

    // MARK: Endings

    private mutating func refused(_ error: HostError) -> [Effect] {
        target = nil
        if Connect.endingCodes.contains(error.code) || Connect.deliveredCodeRefusals.contains(error.code) {
            // The request ended, and is spent: the note offers Start Over.
            screen = .closed
            requestKept = false
            request = nil
            return [
                .cancelPoll, .cancelDeadline,
                .say(.connectEnded(code: error.code, message: error.message)),
                .refreshStatus,
            ]
        }
        if !Connect.keepsRequestCodes.contains(error.code) {
            requestKept = false
        }
        screen = Connect.accountAgainCodes.contains(error.code) ? .account : .closed
        return [
            .cancelPoll, .cancelDeadline,
            .say(.refused(code: error.code, message: error.message, afterSAS: shownSAS)),
            .refreshStatus,
        ]
    }

    /// Close the window on a pair state that ends the flow, with the sentence for it.
    private mutating func closeFor(_ pair: StatusPair) -> [Effect] {
        screen = .closed
        target = nil
        var effects: [Effect] = [.cancelPoll, .cancelDeadline]
        switch pair {
        case .noPair:
            effects.append(.say(.refused(code: "none", message: nil, afterSAS: shownSAS)))
        case .expired:
            effects.append(.say(.refused(code: "expired", message: nil, afterSAS: true)))
        case .refused:
            effects.append(.say(.refused(code: "no_gate", message: nil, afterSAS: shownSAS)))
        case .severalPairs:
            effects.append(.say(.refused(code: "several_pairs", message: nil, afterSAS: shownSAS)))
        case .unreadable(let code):
            effects.append(.say(.refused(code: code, message: nil, afterSAS: shownSAS)))
        case .waiting, .sasReady, .sasPending, .confirmed, .other:
            break
        }
        effects.append(.refreshStatus)
        return effects
    }
}
