import Foundation

/// The numbers and rules the pairing flow works by, and the check on a pasted code.
enum Pairing {
    /// What every pairing code starts with (the pairing library's own prefix).
    static let codePrefix = "swbp1_"
    /// How long the part after the prefix may be: the whole code is then at most 8192 characters,
    /// which with its newline fits `HostCLI.maxInputBytes`. A real code is a few hundred characters
    /// of unpadded base64url; these bounds only stop what plainly is not one.
    static let codeBodyLength = 100...8186
    /// How often `pair status` asks while the window shows the waiting page.
    static let pollInterval: TimeInterval = 5
    /// How long the `pair --code-stdin` child may wait for Lucille before it is interrupted, which
    /// keeps the pair; `pair status` then goes on asking while the window is open.
    static let childTimeLimit: TimeInterval = 900
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
    /// Refusals after which the code window comes back, for another code or another try.
    static let newCodeCodes: Set<String> = ["bad_code", "wrong_hub", "code_has_token", "answer_failed", "hub_error", "busy"]

    private static let codeAlphabet = CharacterSet(
        charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    )

    /// The pasted text with every space and line break taken out: base64url has none, and a
    /// pasted code can carry them.
    static func cleaned(_ text: String) -> String {
        var kept = String.UnicodeScalarView()
        for scalar in text.unicodeScalars where !CharacterSet.whitespacesAndNewlines.contains(scalar) {
            kept.append(scalar)
        }
        return String(kept)
    }

    /// Whether `code` (already `cleaned`) looks like a pairing code: the prefix, then 100 to 8186
    /// characters of the base64url alphabet. The host checks it properly.
    static func looksLikeCode(_ code: String) -> Bool {
        guard code.hasPrefix(codePrefix) else {
            return false
        }
        let body = code.unicodeScalars.dropFirst(codePrefix.unicodeScalars.count)
        guard codeBodyLength.contains(body.count) else {
            return false
        }
        return body.allSatisfy { codeAlphabet.contains($0) }
    }

    /// The hint the code window shows under what was typed or pasted; nil while it is empty or
    /// looks like a code.
    static func hint(for typed: String) -> String? {
        let code = cleaned(typed)
        if code.isEmpty || looksLikeCode(code) {
            return nil
        }
        return ConnectionText.codeHint
    }

    /// One pair's SAS, by the pair's name and its deadline.
    static func key(name: String, deadline: Date) -> String {
        "\(name)|\(Int(deadline.timeIntervalSince1970))"
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
}

/// The pairing flow, decisions/0001's gate: the Mac shows the SAS from `sas_pending`, and the user
/// clicks They Match only if Lucille's page shows the same number (spec/CLI.md §2 and §7). It is a
/// value: inputs in, effects out. It draws nothing and starts nothing; the app's glue runs each
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
        case status(StatusPurpose)
    }

    enum Busy: Equatable, Sendable {
        case checking
        case confirming
        case rejecting
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
        case enterCode
        case waiting(clockSkewed: Bool)
        case compare(Comparison)
        case busy(Busy)
    }

    enum Effect: Equatable, Sendable {
        /// Start `pair --code-stdin --json` with this code on its standard input.
        case startPair(code: String)
        /// SIGINT the pairing child, which keeps the pair.
        case interruptPair
        case run(Verb)
        /// Send `pollDue` after this many seconds.
        case pollAfter(TimeInterval)
        case cancelPoll
        /// Send `deadlinePassed` at this wall-clock time.
        case deadlineAt(Date)
        case cancelDeadline
        /// Empty the code field: the code is in the pipe.
        case clearCodeField
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
        /// Connect, with what the code field holds.
        case submit(String)
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
        case .submit(let typed):
            return submit(typed)
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
    }

    private mutating func startConnecting() -> [Effect] {
        guard !pairChildAlive, !isBusy else {
            return []
        }
        epoch += 1
        beginFlow()
        screen = .enterCode
        return [.cancelRunRetries, .cancelPoll, .cancelDeadline]
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

    private mutating func submit(_ typed: String) -> [Effect] {
        guard case .enterCode = screen, !pairChildAlive else {
            return []
        }
        let code = Pairing.cleaned(typed)
        guard Pairing.looksLikeCode(code) else {
            return []
        }
        screen = .waiting(clockSkewed: false)
        pairChildAlive = true
        lastError = nil
        shownSAS = false
        return [.clearCodeField, .startPair(code: code)]
    }

    // MARK: The pairing child

    private mutating func childObject(_ object: HostObject) -> [Effect] {
        switch object {
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
        if rejectWhenChildEnds {
            rejectWhenChildEnds = false
            return [.run(.pairReject)]
        }
        guard current else {
            return []
        }
        if case .exited(let status) = end, status == 1 || status == 2, let error {
            if error.code == "interrupted" {
                // The pair is kept. While the window is open, `pair status` goes on asking.
                if case .waiting = screen {
                    return [.run(.pairStatus)]
                }
                return [.refreshStatus]
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
        case .status(.beforeConfirm):
            return beforeConfirm(outcome)
        case .status(.afterConfirm):
            return afterConfirm(outcome)
        case .status(.afterChild):
            return afterChild(outcome)
        }
    }

    private mutating func pairStatusResult(_ outcome: VerbOutcome) -> [Effect] {
        if isBusy {
            return []
        }
        switch outcome {
        case .done:
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

    private mutating func stopConnecting() -> [Effect] {
        if isBusy {
            return []
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
        if case .waiting = screen, !pairChildAlive {
            return [.pollAfter(Pairing.pollInterval)]
        }
        return []
    }

    private func pollDue() -> [Effect] {
        guard case .waiting = screen, !pairChildAlive else {
            return []
        }
        return [.run(.pairStatus)]
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
        case .enterCode, .waiting, .compare:
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
        screen = Pairing.newCodeCodes.contains(error.code) ? .enterCode : .closed
        target = nil
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
