import Foundation

// Open at Login's rules (decisions/0002 §3.1 step 10, §3.3 and §9): which ends of a pair take the
// login item away, when a launch counts as paired, when the app says something about the login
// item at launch, and what came of asking macOS to register or unregister it. Foundation only, so
// the self-test runs them without a screen. `LoginItem` asks macOS; nothing here does.

/// The state macOS gives the app's login item (`SMAppService.Status`), as `LoginItem` reads it.
/// The raw value is the name the app writes on standard error ("login item: enabled"), and the
/// only thing it writes of it.
enum LoginState: String, Equatable, Sendable {
    case notRegistered
    case enabled
    case requiresApproval
    case notFound
}

/// What came of asking macOS to open the app at login, by the state it gives afterwards.
enum LoginOutcome: Equatable, Sendable {
    case enabled
    /// Registered, and macOS waits for the user to turn it on in System Settings.
    case requiresApproval
    /// Not registered. `detail` is macOS's own words, if it gave any, for Show Details.
    case failed(detail: String?)
}

/// What came of taking the login item away. Not `none`, which would read as `Optional.none`.
enum LoginRemoval: Equatable, Sendable {
    /// macOS had none to take away: nothing was asked.
    case nothingToRemove
    case removed
    /// macOS still has it after the app asked.
    case failed
}

enum LoginRules {
    /// Whether the pair is gone, so that the login item goes with it: Lucille ended it, it
    /// leaked or expired, another command forgot it, or there is none. A gate that can't open
    /// (`noGate`) keeps the pair until Stop Connecting, and an exit 20 whose reason this release does
    /// not know ends nothing. Every case is named, so a new one must be decided here.
    static func pairGone(_ why: NotConnected) -> Bool {
        switch why {
        case .notPaired, .leaked, .expired, .ended, .forgotten:
            return true
        case .noGate, .other:
            return false
        }
    }

    /// Whether a note of the pairing flow says the pair is gone: `pair reject` forgot it, or the
    /// host found none. A pair whose gate record was lost (`refused`, `no_gate`) may have the
    /// login item, and it is ended by Stop Connecting, `pair reject`, not by Disconnect. `pair
    /// reject` refuses a confirmed pair (`already_confirmed`), so no pair kept here is lost.
    static func pairForgotten(_ note: PairingNote) -> Bool {
        switch note {
        case .rejected:
            return true
        case .refused(let code, _, _):
            return code == "none"
        case .confirmed, .confirmedAnother, .notConfirmed, .confirmUnknown, .rejectFailed, .notStarted:
            return false
        }
    }

    /// Whether the user's click may ask macOS to open the app at login: only while a pair is
    /// confirmed, and never once the supervisor has halted because the Mac is not connected,
    /// which an exit of `run` says before `status` is read again.
    static func mayRegister(confirmed: Bool, phase: RunSupervisor.Phase) -> Bool {
        if case .halted(.notConnected) = phase {
            return false
        }
        return confirmed
    }

    /// Whether a launch is paired: only a confirmed pair counts. A pairing not finished, one that
    /// ended, a store that can't be read, or no `status` at all, is not paired, and the app says
    /// where its menu is.
    static func isPaired(_ pair: StatusPair?) -> Bool {
        if case .confirmed? = pair {
            return true
        }
        return false
    }

    /// Whether the app says at launch that it won't start when the user logs in: only while
    /// paired, and only when macOS waits for the user's approval. Nothing for `notRegistered`,
    /// which may be the user's own choice.
    static func launchNote(paired: Bool, state: LoginState) -> Bool {
        paired && state == .requiresApproval
    }

    /// What came of a register, from the state macOS gives afterwards: a throw counts only when
    /// the item is neither on nor waiting for approval, and then its words are the detail.
    static func afterRegister(state: LoginState, error: String?) -> LoginOutcome {
        switch state {
        case .enabled:
            return .enabled
        case .requiresApproval:
            return .requiresApproval
        case .notRegistered, .notFound:
            return .failed(detail: error)
        }
    }

    /// What came of an unregister, from the state before and after: nothing to take away unless
    /// the item was on or waiting for approval; taken away once macOS no longer has it.
    static func removal(before: LoginState, after: LoginState) -> LoginRemoval {
        guard before == .enabled || before == .requiresApproval else {
            return .nothingToRemove
        }
        if after == .notRegistered || after == .notFound {
            return .removed
        }
        return .failed
    }
}

/// Open at Login's words. The app's name is read from its Info.plist (`HostText.appName`).
enum LoginText {
    private static let appName = HostText.appName

    /// The menu's submenu while connected, and its switch.
    static let settingsMenu = "Settings"
    static let openAtLoginItem = "Open at Login"

    static let openSettingsButton = "Open Login Items Settings"
    static let keepItOffButton = "Keep It Off"

    /// After Done, or the switch, when macOS waits for the user's approval.
    static let needsApprovalTitle = "macOS needs your OK before \(appName) can open when you log in"
    static let needsApprovalText = "Turn \(appName) on in System Settings › General › Login Items & Extensions. Until then, Lucille can reach this Mac only while \(appName) is open."

    /// After Done, or the switch, when macOS did not register it.
    static let couldNotTurnOnTitle = "\(appName) couldn't turn on opening at login"
    static let couldNotTurnOnText = "Open \(appName) yourself after you log in: Lucille can reach this Mac only while \(appName) is open. To try again, choose Settings, then Open at Login, in \(appName)'s menu."

    /// The switch turned off, and macOS still has the item.
    static let couldNotTurnOffTitle = "\(appName) couldn't stop opening when you log in"
    static let turnOffByHand = "Turn it off in System Settings › General › Login Items & Extensions."

    /// At launch, while paired, when macOS waits for the user's approval (decisions/0002 §3.3).
    static let launchOffTitle = "\(appName) won't start when you log in"
    static let launchOffText = "Opening at login is turned off in System Settings. Lucille can reach this Mac only while \(appName) is open."

    /// What Disconnect's note adds: true whether the item was on or only waited for the user's
    /// approval.
    static let offAfterDisconnect = "\(appName) won't open when you log in."
    static let removeByHand = "\(appName) couldn't turn off opening at login. Turn it off in System Settings › General › Login Items & Extensions."

    /// The sentence Disconnect's note adds for what became of the login item: none when there
    /// was none.
    static func afterDisconnect(_ removal: LoginRemoval) -> String? {
        switch removal {
        case .nothingToRemove:
            return nil
        case .removed:
            return offAfterDisconnect
        case .failed:
            return removeByHand
        }
    }
}
