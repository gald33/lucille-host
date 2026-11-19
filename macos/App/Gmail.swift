import Foundation

// The Gmail window's rules and words (decisions/0003, "Second use", and decisions/0002): what it
// asks the host to do, and what it says. Foundation only, so the self-test runs them without a
// screen. The window itself is `GmailPanel`; `HostModel` runs the verbs.

/// The one Gmail credential the app keeps: `gmail/default`, sent only to imap.gmail.com, its
/// value the account's address, one space, and the app password's 16 letters.
enum GmailRules {
    static let service = "gmail"
    static let name = "default"
    static let host = "imap.gmail.com"
    /// The longest address the host takes (decisions/0003, "The credential").
    static let addressMax = 254
    /// How long List, Save and Remove may take: each opens the Keychain item that keeps the
    /// credentials, and macOS may ask the user about it first, as for the store's other verbs.
    static let keychainTimeout: TimeInterval = 120
    /// How long Test may take: that, and gmail.check's child, which may take 30 s of its own to
    /// sign in and out.
    static let testTimeout: TimeInterval = 150

    /// The form the host takes, matched whole: the address, one space, 16 lowercase letters.
    private static let form = "^[A-Za-z0-9._%+-]{1,64}@(([A-Za-z0-9]|[A-Za-z0-9][A-Za-z0-9-]{0,61}[A-Za-z0-9])\\.){1,8}[A-Za-z]{2,63} [a-z]{16}$"

    private static func inForm(_ value: String) -> Bool {
        guard let regex = try? NSRegularExpression(pattern: form) else {
            return false
        }
        return regex.firstMatch(in: value, range: NSRange(value.startIndex..., in: value)) != nil
    }

    /// The value Save sends on standard input, from what the user typed: the address as typed,
    /// with the spaces around it taken off, and the app password with its spaces taken off and
    /// in lowercase, as Google shows it in four groups of four. Nil when it is not in the form
    /// the host takes, so a mistyped one, or an account's own password, is never saved.
    static func value(address: String, password: String) -> String? {
        let mail = address.trimmingCharacters(in: .whitespacesAndNewlines)
        let letters = password.filter { !$0.isWhitespace }.lowercased()
        let value = "\(mail) \(letters)"
        guard mail.count <= addressMax, value.unicodeScalars.allSatisfy({ $0.isASCII }), inForm(value) else {
            return nil
        }
        return value
    }

    /// `credential list`: whether gmail/default is kept, and when it was saved.
    static let listArguments = ["--json", "credential", "list"]
    /// `credential set`, its value on standard input only, replacing one kept already.
    static let saveArguments = [
        "--json", "credential", "set", service, name, "--host", host, "--value-stdin", "--replace",
    ]
    /// `credential test`: sign in and out, reading nothing.
    static let testArguments = ["--json", "credential", "test", service, name]
    /// `credential remove`.
    static let removeArguments = ["--json", "credential", "remove", service, name]

    /// What Save writes on standard input: the value and a newline.
    static func input(_ value: String) -> Data {
        Data((value + "\n").utf8)
    }

    /// The Gmail credential among those `credential list` reports, if one is kept.
    static func saved(_ credentials: [SavedCredential]) -> SavedCredential? {
        credentials.first { $0.service == service && $0.name == name }
    }
}

/// The Gmail window's words.
enum GmailText {
    private static let appName = HostText.appName

    static let menuItem = "Gmail\u{2026}"
    static let title = "Gmail"
    static let intro = "Lucille can search and read your Gmail through \(appName), once you turn on its Gmail abilities. \(appName) signs in with an app password, which Google makes for one app. It is kept in this Mac's Keychain and sent only to Google's mail server, imap.gmail.com. What Lucille asks to read goes to Lucille."
    static let howTo = "To make one, your Google Account needs 2-Step Verification. On Google's App Passwords page, make a new app password and copy its 16 letters here. Work and school accounts may not offer app passwords. Google turns them off when your account's password changes."
    static let addressLabel = "Gmail address"
    static let passwordLabel = "App password"
    static let addressPlaceholder = "you@gmail.com"
    static let passwordPlaceholder = "16 letters"
    static let saveButton = "Save"
    static let testButton = "Test"
    static let removeButton = "Remove"
    static let openPageButton = "Open Google's App Passwords Page"
    static let closeButton = "Close"

    static let checking = "Checking\u{2026}"
    static let saving = "Saving\u{2026}"
    static let testing = "Signing in to Gmail\u{2026}"
    static let removing = "Removing\u{2026}"

    /// What the window says of the credential kept, or that none is.
    static func kept(_ saved: SavedCredential?) -> String {
        guard let saved else {
            return "No app password is saved."
        }
        let formatter = DateFormatter()
        formatter.dateStyle = .medium
        formatter.timeStyle = .short
        return "An app password is saved (\(formatter.string(from: saved.savedAt)))."
    }

    static let notInForm = "Enter your Gmail address and the app password's 16 letters. Your account's own password won't work here."
    static let saved = "Saved. Choose Test to check that Gmail accepts it."
    static let signedIn = "Gmail accepted the app password."
    static let removed = "The app password was removed from this Mac."
    static let noneToRemove = "No app password was saved."
    static let noneToTest = "Save an app password first."

    /// What the window says when a verb was refused, chosen by the refusal's code, never by its
    /// words. A code this release does not know is the app's own bug.
    static func refusal(_ error: HostError) -> String {
        switch error.code {
        case "sign_in_refused":
            return "Gmail didn't accept the app password. Make a new one on Google's App Passwords page and save it here. After a refusal, \(appName) waits 15 minutes before it signs in with the same one again."
        case "bad_credential":
            return "The saved app password isn't in the form Gmail takes. Save it again."
        case "unreachable":
            return "\(appName) couldn't reach Gmail. Check that this Mac is online, then try again."
        case "limit_reached":
            return "\(appName) signed in to Gmail too many times in the last 10 minutes. Try again later."
        case "paused":
            return "\(appName) is paused, so it signs in to nothing. Resume it, then try again."
        case "no_such_credential":
            return noneToTest
        case "insecure_keyring", "store_error":
            return "\(appName) couldn't use this Mac's Keychain. Try again; if it keeps happening, restart your Mac."
        default:
            return ConnectionText.appBug
        }
    }
}
