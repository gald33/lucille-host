import Foundation
import ServiceManagement

/// The app's login item: the app itself, `SMAppService.mainApp` (decisions/0002 §9), and no
/// helper, agent or daemon. This is the only file that names `SMAppService`; `LoginRules` decides
/// what each answer means.
///
/// - Every function here is synchronous, so each call it makes to `SMAppService` is made from
///   synchronous code. `unregister()` also has an async form (made from
///   `unregister(completionHandler:)`), which a call in an async context would take, and which
///   would not compile without `await`; called from here, it is always the synchronous one. Call
///   these only from synchronous code too, never from a task or an async function: `Connection`
///   calls them from its main-actor methods and from its notes' `done` closures, and, once
///   `forget` is done, from a method of its own (`loginItemAfterForget`), not from the task.
/// - Each does nothing unless this copy is the one at /Applications/<name>.app (`Placement`):
///   `mainApp` is keyed by the bundle identifier, which a "<name> 2" shares, so another copy could
///   otherwise change, or take away, the real copy's item.
/// - Each that acts or checks writes one line on standard error, the state's name and nothing
///   else ("login item: enabled"), from `said`. The plain read, which the menu makes each time it
///   opens, writes nothing.
/// - Registering widens what can happen, so it is asked for only from the user's click: Done on
///   Ready with its box checked, and the Settings menu's Open at Login. Taking it away only
///   narrows, so it needs no click, and happens on its own when the pair ends.
enum LoginItem {
    /// The state macOS gives the login item now. It writes nothing.
    static var state: LoginState {
        guard Placement.current.isPlaced else {
            return .notRegistered
        }
        switch SMAppService.mainApp.status {
        case .notRegistered:
            return .notRegistered
        case .enabled:
            return .enabled
        case .requiresApproval:
            return .requiresApproval
        case .notFound:
            return .notFound
        @unknown default:
            return .notFound
        }
    }

    /// The state at launch, said on standard error.
    static func checked() -> LoginState {
        guard Placement.current.isPlaced else {
            return .notRegistered
        }
        return said(state)
    }

    /// Ask macOS to open the app when the user logs in, then go by the state it gives.
    static func register() -> LoginOutcome {
        guard Placement.current.isPlaced else {
            return .failed(detail: nil)
        }
        var error: String?
        do {
            try SMAppService.mainApp.register()
        } catch let thrown {
            error = thrown.localizedDescription
        }
        return LoginRules.afterRegister(state: said(state), error: error)
    }

    /// Take the login item away, only when macOS has it on or waiting for approval, then go by the
    /// state it gives.
    static func unregisterIfRegistered() -> LoginRemoval {
        guard Placement.current.isPlaced else {
            return .nothingToRemove
        }
        let before = state
        guard before == .enabled || before == .requiresApproval else {
            _ = said(before)
            return .nothingToRemove
        }
        do {
            try SMAppService.mainApp.unregister()
        } catch {
            // What came of it is the state macOS gives afterwards, below.
        }
        return LoginRules.removal(before: before, after: said(state))
    }

    /// System Settings, at Login Items & Extensions, where the user turns the item on or off.
    static func openSettings() {
        guard Placement.current.isPlaced else {
            return
        }
        SMAppService.openSystemSettingsLoginItems()
    }

    /// The one line on standard error: the state's name and nothing else.
    private static func said(_ state: LoginState) -> LoginState {
        Connection.log("login item: \(state.rawValue)")
        return state
    }
}
