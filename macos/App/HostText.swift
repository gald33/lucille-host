import Foundation

/// One catalog operation as `lucille-host ops list --json` writes it (spec/CLI.md §6, `op`), or a
/// name the local policy holds that this release's catalog does not have. The menu calls them
/// abilities, and says each in the core's words (§8).
struct Ability: Equatable, Identifiable, Sendable {
    /// Its state, closed within version 1 of the contract: an `op` object in a state this release
    /// does not know is one it cannot read (`HostObject.readOp`), never a guess.
    enum State: Equatable, Sendable {
        case enabled
        case disabled
        /// An update changed it, so it is off until it is turned on again.
        case disabledByUpgrade
        /// This Mac cannot run it: the program it needs is not there.
        case unavailable(missingExecutable: String)
        /// The local policy names it, and this release does not have it.
        case notInCatalog
    }

    let name: String
    let state: State
    /// The switch's position, as the host says it: the local policy turns it on, whether or not
    /// this Mac can run it.
    let enabled: Bool
    /// The core's few words for what it does; nil only for a name this release does not have.
    let summary: String?
    /// The core's sentence on what Lucille gets when it runs; nil only for a name this release
    /// does not have.
    let sends: String?
    /// A reply can carry the contents of a file. The app asks before it turns such an ability on.
    let sendsFileContent: Bool
    /// Meant for testing, as `host.sleep` is.
    let diagnostic: Bool

    var id: String { name }

    /// What the menu calls it: the core's few words, or its name if the core has none.
    var title: String { summary ?? name }

    /// The sentence the menu shows under it: what Lucille gets while it is on, in the core's words.
    var detail: String? { sends }

    /// Whether the Abilities menu lists it. Operations meant for testing are left out, and so are
    /// `host.describe`, which is on from the start and is described in the menu instead, and a
    /// name the policy holds that this release does not have.
    var isShown: Bool {
        !diagnostic && state != .notInCatalog && !HostText.unlistedAbilities.contains(name)
    }

    /// Shown with a checkmark: the host says the local policy turns it on.
    var isOn: Bool { enabled }

    /// Whether the menu asks before it switches it: only before it turns on an ability whose
    /// replies can carry a file's contents, decided on the host's `sends_file_content`, never on
    /// its words (spec/CLI.md §6; decisions/0002 §13). Turning one off asks nothing.
    func asksFirst(turningOn on: Bool) -> Bool {
        on && sendsFileContent
    }

    /// Whether the menu may switch it: off when it is on, and on when this Mac can run it.
    var canChange: Bool {
        switch state {
        case .enabled, .disabled, .disabledByUpgrade:
            return true
        case .unavailable:
            return enabled
        case .notInCatalog:
            return false
        }
    }

    /// A plain sentence for a state that is more than on or off.
    var note: String? {
        switch state {
        case .enabled, .disabled:
            return nil
        case .disabledByUpgrade:
            return "An update changed this ability, so it was turned off."
        case .unavailable:
            return enabled ? "On, but this Mac lacks a program it needs." : "This Mac lacks a program it needs."
        case .notInCatalog:
            return "Not in this version of \(HostText.appName)."
        }
    }
}

/// One folder alias as `lucille-host roots list --json` writes it (spec/CLI.md §6, `root`).
struct Folder: Equatable, Identifiable, Sendable {
    let alias: String
    let path: String
    /// It is the home folder, holds it, or is `/`, and was added once the user confirmed it.
    let broad: Bool

    var id: String { alias }
}

/// Whether the host can open one folder, as `lucille-host roots check --json` writes it
/// (spec/CLI.md §6 and §9.2, `root_check`). Its state is closed within version 1, as an
/// ability's is. The menu runs no check yet.
struct FolderCheck: Equatable, Sendable {
    enum State: Equatable, Sendable {
        case ok
        /// macOS's privacy protection refuses the host this folder.
        case deniedByMacOS
        /// The folder's permissions refuse it.
        case denied
        /// Nothing is there, or it is not a folder.
        case missing
        /// Any other error, by its name, such as `EIO`.
        case error(errno: String)
    }

    let alias: String
    let path: String
    let state: State
}

/// What the app tells the user about a run that failed: one short sentence, and the host's own
/// words, if it gave any, as a detail.
struct Explanation: Equatable, Sendable {
    let message: String
    let detail: String?
    /// Whether the note that shows it offers Reset Settings…: only when the host refused
    /// `policy_invalid`, whose sentence says that the settings file can't be read (decisions/0002
    /// §14).
    var offersReset = false
}

/// A folder the app adds only once the user confirms, because the host adds it only with
/// `--allow-home-or-root`. The whole disk is not one: the app refuses it outright
/// (`HostText.isWholeDisk`).
enum BroadFolder: Equatable, Sendable {
    case home
    case holdsHome
}

/// What the app itself can see of a folder the host could not add.
enum FolderAccess: Equatable, Sendable {
    /// The app sees the folder, so what stopped the host is macOS's privacy protection.
    case visible
    /// macOS refuses the app too.
    case blocked
    /// The folder is not there, or is not a folder.
    case missing
}

/// Where a folder is, when that changes what the app says before it adds the folder.
struct FolderPlace: Equatable, Sendable {
    /// The folder macOS protects that holds it: "Documents", "Desktop" or "Downloads".
    var protectedFolder: String?
    /// It is on another drive (under /Volumes): an external, removable or network one.
    var onOtherDrive = false
    /// It is in iCloud Drive or another cloud folder, or macOS syncs it to iCloud.
    var inCloud = false
}

/// The menu's words; the command lines of `roots add` and `roots add --suggest-alias`; and how the
/// results of `--version`, `ops` and `roots`, which `HostModel` runs with `--json` first
/// (spec/CLI.md §3 and §7), are read: through `HostJSON`, as every other verb's are. A refusal is
/// read by its `error` object's `code`, never by its words, which are for people (§2): they are
/// shown as the details. Only a usage error, and a run that wrote no `error` object, are said with
/// the lines on standard error. The self-test checks this against the real command line on every
/// build.
enum HostText {
    private static let program = "lucille-host"

    /// Operations the Abilities menu does not list, besides those meant for testing (the core's
    /// `diagnostic`): `host.describe`, which is on from the start and is described in the menu
    /// instead.
    static let unlistedAbilities: Set<String> = ["host.describe"]

    /// The app's name, from its Info.plist, where build_app.py puts the name tools/macos/pins.toml
    /// holds, so that a new name needs no change here. Code run outside the app, such as the
    /// self-test, finds no name there and uses "the app". That fallback is deliberately not the
    /// app's name: if the app ever stopped reading its Info.plist, the `ui` check, which looks for
    /// the name from pins.toml, would fail instead of passing on the fallback.
    static let appName = Bundle.main.object(forInfoDictionaryKey: "CFBundleDisplayName") as? String ?? "the app"

    /// What the app is for, for someone who does not know it. The second sentence is there
    /// because `host.describe` is on from the start, and the menu has no switch for it.
    static let appPurpose = "\(appName) lets Lucille use only the folders you choose on this Mac, and only for the abilities you turn on. Lucille can also see which abilities are on, and the names you gave your folders."

    /// What the app says when it opens, and when it is opened again while it runs: it has no
    /// window and no Dock icon, so otherwise nothing would seem to happen.
    static let welcomeTitle = "\(appName) is in your menu bar"
    static let welcomeText = """
        \(appPurpose)

        Click its icon near the top right of your screen, by the clock: a small computer, or a \
        pause sign while Lucille is paused. Everything \(appName) does is in that menu.

        If you can't see the icon, your menu bar may be too full. Quit another app that has an \
        icon there, and it will show.
        """

    /// The menu's last item, and the second button of the note the app shows when it opens.
    static let quitApp = "Quit \(appName)"

    /// The menu bar item's name, which VoiceOver reads.
    static func statusItemName(paused: Bool) -> String {
        paused ? "\(appName), paused" : appName
    }

    /// The menu's line while Lucille is paused.
    static let pausedNote = "Lucille's requests are turned away until you resume."

    /// What the app says once a folder is added. `connected` is whether this Mac holds a pair,
    /// confirmed or being confirmed. Without one, a new pairing sets the policy aside (spec/CLI.md
    /// §6, `policy_reset`), so the folder is of use only once it is added again after that.
    static func folderAdded(alias: String, connected: Bool = false) -> String {
        if connected {
            return "Lucille can use \u{201C}\(alias)\u{201D} for the abilities you turn on."
        }
        return "Once this Mac is connected to Lucille, Lucille can use \u{201C}\(alias)\u{201D} only after you add it again, because a new connection starts with no folders."
    }

    /// What the app says when macOS's privacy protection kept the host from a folder.
    static let blockedByMacOS = "macOS is blocking \(appName) from this folder. To allow it, open System Settings, choose Privacy & Security, then Files & Folders, and turn on the switch for this folder's location under \(appName). Then add the folder again."

    /// What the app says while a run it stopped waiting for, of a verb that takes the host's policy
    /// lock, has still not ended (`HostCLI.stuckRuns`): it may hold that lock, so every change to
    /// folders and abilities waits for it.
    static let stuckRun = "A folder check is still stuck, probably on a network or external drive that stopped answering. Until it ends, \(appName) can't change its folders or abilities. Reconnect the drive, or restart your Mac, then try again."

    /// The most folders the host's policy holds (spec/CLI.md §10, `too_many_roots`).
    static let maxFolders = 64

    /// Whether `roots add` refused because the folder is the home folder, holds it, or is "/":
    /// the code `broad_folder`. The user may then confirm, unless it is the whole disk, and the
    /// app adds the flag that means it (`addFolderArguments`).
    static func needsHomeConfirmation(_ result: CLIResult) -> Bool {
        if case .refused(let error) = VerbOutcome(result) {
            return error.code == "broad_folder"
        }
        return false
    }

    /// Which question to ask about a folder the host refused as `broad_folder`, by the error's
    /// `kind` (spec/CLI.md §5): `home` is the home folder itself, by whatever path it was chosen;
    /// any other is a folder that holds it. The whole disk, `root`, is refused before this is
    /// asked (`isWholeDisk`).
    static func refusedBroadFolder(_ result: CLIResult) -> BroadFolder {
        if case .refused(let error) = VerbOutcome(result), error.code == "broad_folder", error.kind == "home" {
            return .home
        }
        return .holdsHome
    }

    /// Whether `roots add` refused because a folder already has the alias. The host's suggestion
    /// takes no lock, so another verb may have taken the alias since (spec/CLI.md §7): the app
    /// then asks for a name again.
    static func nameTaken(_ result: CLIResult) -> Bool {
        if case .refused(let error) = VerbOutcome(result) {
            return error.code == "alias_taken"
        }
        return false
    }

    /// The whole disk, which the app never adds, whatever the user chooses: the top of the
    /// startup disk, the Data volume that holds every user's files, and the two folders between
    /// them, which hold that volume. The command line still adds each of them with
    /// `--allow-home-or-root`; the app never passes it for them (`addFolderArguments`).
    static let wholeDisk = ["/", "/System", "/System/Volumes", "/System/Volumes/Data"]

    /// Whether `path` is the whole disk: one of `wholeDisk` as written or with its links
    /// followed, in any case, or the same folder as one of them by device and inode. It reads the
    /// disk, on the caller's thread, only when the path as written names none of them: it follows
    /// the path's links once, and reads the device and inode of what they lead to.
    static func isWholeDisk(path: String) -> Bool {
        if namesWholeDisk(path) {
            return true
        }
        let real = URL(fileURLWithPath: path).resolvingSymlinksInPath().path
        if namesWholeDisk(real) {
            return true
        }
        guard let identity = fileIdentity(real) else {
            return false
        }
        return wholeDisk.contains { fileIdentity($0) == identity }
    }

    /// Whether `path` names one of `wholeDisk`, in any case, without reading the disk: Set Up tests
    /// every folder of a policy set aside by it, whatever that policy marked it.
    static func namesWholeDisk(_ path: String) -> Bool {
        let spelling = standardized(path)
        return wholeDisk.contains { spelling.caseInsensitiveCompare($0) == .orderedSame }
    }

    /// The `roots add` command line for a folder: `--json` first, and `--` before the alias and
    /// the path, so that neither is ever read as an option (spec/CLI.md §3 and §7).
    /// `--allow-home-or-root` is there, before `--`, only when the user confirmed a home folder or
    /// a folder holding it, and never for the whole disk, whatever was confirmed. It reads the disk
    /// (`isWholeDisk`) only for a confirmed folder, which `addFolder` has just read, so any other
    /// folder gets no read before the run and its window with Cancel.
    static func addFolderArguments(alias: String, path: String, confirmedHome: Bool) -> [String] {
        var verb = ["--json", "roots", "add"]
        if confirmedHome && !isWholeDisk(path: path) {
            verb.append("--allow-home-or-root")
        }
        verb += ["--", alias, path]
        return verb
    }

    /// The command line that asks the host for a folder's alias (spec/CLI.md §9.1). It takes no
    /// `--`, so a path that starts with "-" is written `--suggest-alias=<path>`, which the host
    /// reads as the path (§7).
    static func suggestAliasArguments(path: String) -> [String] {
        path.hasPrefix("-")
            ? ["--json", "roots", "add", "--suggest-alias=" + path]
            : ["--json", "roots", "add", "--suggest-alias", path]
    }

    /// The name the name dialog starts from: the host's suggestion, when it gave one the app can
    /// use, which is a valid alias none of the app's folders has; else the app's own, made from
    /// the folder's name. The user is not told which it is.
    static func suggestion(_ outcome: VerbOutcome, folderName: String, taken: Set<String>) -> String {
        if let alias = outcome.aliasSuggestion, Alias.isValid(alias), !taken.contains(alias) {
            return alias
        }
        return Alias.suggest(forFolderName: folderName, taken: taken)
    }

    /// Whether `path` is one the app asks about before it adds it, as far as it can tell from the
    /// path: the home folder, or a folder that holds it. The whole disk holds it too, but is not
    /// asked about: the app refuses it. The host decides; this lets the app ask before the user
    /// names the folder.
    static func broadFolder(path: String, home: String) -> BroadFolder? {
        let folder = standardized(path)
        let homeFolder = standardized(home)
        let kind: BroadFolder
        if folder == homeFolder {
            kind = .home
        } else if homeFolder.hasPrefix(folder + "/") {
            kind = .holdsHome
        } else {
            return nil
        }
        // Only a folder that is the home folder or holds it, by its path, is read from the disk.
        return isWholeDisk(path: path) ? nil : kind
    }

    /// What the home folder holds, as the app's questions about it say.
    private static let homeContents = "your Documents, Desktop and Downloads folders, your Library, and other apps' files"

    /// The question the app asks before it adds the home folder, or a folder that holds it: a
    /// title and a sentence for each kind.
    static func broadFolderQuestion(_ kind: BroadFolder) -> (title: String, text: String) {
        switch kind {
        case .home:
            return (
                "Add your whole home folder?",
                "This is your home folder. It holds \(homeContents). Abilities you turn on could use any file in it. Most people choose one folder inside it instead."
            )
        case .holdsHome:
            return (
                "Add a folder that holds your home folder?",
                "This folder holds your home folder, with \(homeContents). Abilities you turn on could use any file in it. Most people choose one smaller folder instead."
            )
        }
    }

    /// What the app says when the user chooses the whole disk, which it never adds: a title and
    /// a sentence. `diskName` is what Finder calls the startup disk.
    static func wholeDiskRefusal(diskName: String) -> (title: String, text: String) {
        (
            "\(appName) can't use your whole disk",
            "This folder holds every user's files on \(diskName), your startup disk: your home folder, with \(homeContents), and the files of everyone else who uses this Mac. \(appName) never lets Lucille use all of that. Choose one folder instead."
        )
    }

    /// Where the folder at `path` is, as far as it changes what the app says first. `synced` is
    /// whether macOS says the folder is kept in iCloud.
    static func place(of path: String, home: String, synced: Bool = false) -> FolderPlace {
        let folder = standardized(path)
        let homeFolder = standardized(home)
        func inside(_ parent: String) -> Bool {
            folder == parent || folder.hasPrefix(parent + "/")
        }
        var place = FolderPlace()
        place.protectedFolder = ["Documents", "Desktop", "Downloads"].first { inside(homeFolder + "/" + $0) }
        place.onOtherDrive = folder.hasPrefix("/Volumes/")
        place.inCloud = synced
            || inside(homeFolder + "/Library/Mobile Documents")
            || inside(homeFolder + "/Library/CloudStorage")
        return place
    }

    /// The name dialog's text, with the sentence `note` first when the app had to change the name,
    /// and, last, what macOS may ask once the user adds a folder from `place`.
    static func aliasPrompt(folderName: String, note: String?, place: FolderPlace = FolderPlace()) -> String {
        let purpose = "Lucille will know \u{201C}\(folderName)\u{201D} only by this name. It never learns where the folder is on your Mac."
        let rules = "Use lowercase letters, digits, - and _, up to \(Alias.maxLength) characters."
        var ask: String?
        if let protected = place.protectedFolder {
            ask = "macOS may then ask whether \(appName) can access files in your \(protected) folder. Click Allow, or the folder can't be added."
        } else if place.onOtherDrive {
            ask = "macOS may then ask whether \(appName) can access files on a removable or network volume. Click Allow, or the folder can't be added."
        }
        let cloud = place.inCloud
            ? "This folder is in iCloud Drive or another cloud folder. Folders there may not work with \(appName) yet."
            : nil
        return [note, purpose, rules, ask, cloud].compactMap { $0 }.joined(separator: "\n\n")
    }

    /// What the app itself can see of the folder at `path`.
    static func access(of path: String) -> FolderAccess {
        var isFolder: ObjCBool = false
        if FileManager.default.fileExists(atPath: path, isDirectory: &isFolder) {
            return isFolder.boolValue ? .visible : .missing
        }
        do {
            _ = try FileManager.default.attributesOfItem(atPath: path)
            return .missing
        } catch let error as CocoaError where error.code == .fileReadNoPermission {
            return .blocked
        } catch {
            return .missing
        }
    }

    /// What a run was for, when that changes what the user is told.
    enum Activity: Equatable, Sendable {
        /// Adding a folder under the alias the app asked for, with what the app itself can see of
        /// the folder.
        case addingFolder(FolderAccess, alias: String)
        case other
    }

    /// One plain sentence for each code `--version`, `ops`, `roots` and `policy previous` can refuse
    /// with (spec/CLI.md §7 and §10), but `alias_taken` and a `not_a_folder` the app can see, which say
    /// more while a folder is added (`refusal`), and the codes that can only be the app's own
    /// mistake (`ConnectionText.appBugCodes`), which read as `ConnectionText.appBug`.
    static let refusals: [String: String] = [
        "policy_invalid": "\(appName)'s settings file can't be read.",
        "policy_lock_failed": "\(appName) couldn't lock its settings file, so nothing was changed. Try again.",
        "not_in_catalog": "This version of \(appName) doesn't have that ability.",
        "unavailable": "This Mac lacks a program that ability needs.",
        "not_enabled": "That ability was already off.",
        "bad_alias": "That name can't be used.",
        "too_many_roots": "\(appName) can use at most \(maxFolders) folders. Remove one before you add another.",
        "not_a_folder": "That folder is no longer there.",
        "own_folder": "That folder holds \(appName)'s own settings, so it can't be added.",
        "on_proc": "That folder can't be used.",
        "broad_folder": "That folder is your home folder, holds it, or is your whole disk.",
        "unstorable_path": "That folder's location can't be stored in \(appName)'s settings.",
        "no_such_alias": "That folder was already removed.",
        "previous_invalid": "\(appName) can't read the folders and abilities you had before, so it can't offer them back.",
    ]

    /// The sentence for a refusal, chosen by its code. While a folder is added, a name in use
    /// names the alias the app asked for, never one read from the host's words; and a folder the
    /// host found none of, which the app can see or is refused too, is macOS's privacy protection
    /// at work: the host reads a folder macOS keeps from it as no folder at all.
    static func refusal(_ error: HostError, during activity: Activity = .other) -> String {
        if case .addingFolder(let access, let alias) = activity {
            switch error.code {
            case "alias_taken":
                return "You already have a folder named \(alias). Choose another name."
            case "not_a_folder" where access != .missing:
                return blockedByMacOS
            default:
                break
            }
        }
        return refusals[error.code] ?? ConnectionText.appBug
    }

    /// The refusal that says an ability being turned off is off already.
    static let alreadyOff: Set<String> = ["not_enabled"]

    /// The refusal that says a folder being removed is gone already.
    static let alreadyRemoved: Set<String> = ["no_such_alias"]

    /// Whether `result` is a refusal whose code is one of `codes`: things are already as the user
    /// asked, so the menu shows no note for it. Decided on the code alone, never on the words.
    static func alreadyDone(_ result: CLIResult, codes: Set<String>) -> Bool {
        if case .refused(let error) = VerbOutcome(result) {
            return codes.contains(error.code)
        }
        return false
    }

    /// The button that turns on an ability whose replies can carry a file's contents, once the
    /// user has read what Lucille gets (decisions/0002 §13).
    static let turnOnButton = "I Understand, Turn On"

    /// The question the app asks before it turns on such an ability: its name and what Lucille
    /// gets, both in the core's words.
    static func turnOnQuestion(_ ability: Ability) -> (title: String, text: String) {
        ("Turn on \u{201C}\(ability.title)\u{201D}?", ability.detail ?? "")
    }

    /// What to tell the user when a run failed: a plain sentence for each refusal, by its code, and
    /// the host's own words as the detail. `earlierRunStuck` is whether a run the app stopped
    /// waiting for, of a verb that takes the policy lock, has still not ended (`HostCLI.stuckRuns`).
    static func explain(
        _ result: CLIResult, during activity: Activity = .other, earlierRunStuck: Bool = false
    ) -> Explanation {
        if result.status == CLIResult.notStarted {
            return Explanation(message: "\(appName) is damaged. Download it again.", detail: result.stderr)
        }
        if result.status == CLIResult.notFinished || (earlierRunStuck && (result.timedOut || result.cancelled)) {
            return Explanation(message: stuckRun, detail: nil)
        }
        if result.timedOut {
            let message: String
            switch activity {
            case .addingFolder:
                message = "Checking this folder took too long. If macOS asked whether \(appName) can access it, answer, then add the folder again. If it is on a network or external drive, check that it is connected."
            case .other:
                message = "\(appName)'s helper program did not answer in time, so it was stopped."
            }
            return Explanation(message: message, detail: nil)
        }
        if result.cancelled {
            return Explanation(message: "Stopped.", detail: nil)
        }
        if case .refused(let error) = VerbOutcome(result) {
            // Reset Settings… answers only an invalid policy file: the policy a pairing set aside
            // is another file, which `policy reset` never touches (spec/CLI.md §7).
            return Explanation(
                message: refusal(error, during: activity),
                detail: details(of: error, stderr: result.stderr),
                offersReset: error.code == "policy_invalid"
            )
        }
        // A run that wrote no error object, a signal say, has only standard error to show.
        let reason = reasons(result.stderr)
        return Explanation(
            message: ConnectionText.appBug,
            detail: reason.isEmpty ? "It stopped with exit status \(result.status)." : reason
        )
    }

    /// The host's words for a refusal: its sentence, the error object's `message`; for a usage
    /// error, whose sentence only points to standard error, the parser's words there.
    private static func details(of error: HostError, stderr: String) -> String {
        if error.code == "usage" {
            let words = reasons(stderr)
            if !words.isEmpty {
                return words
            }
        }
        return error.message
    }

    /// The host's reason for a refusal, from its stderr: the lines that are not a usage summary,
    /// without the program's name, in the host's own words and case.
    static func reasons(_ stderr: String) -> String {
        var found: [String] = []
        for raw in stderr.split(separator: "\n", omittingEmptySubsequences: true) {
            if raw.hasPrefix(" ") || raw.hasPrefix("usage:") {
                continue
            }
            var line = raw.trimmingCharacters(in: .whitespaces)
            if let marker = line.range(of: ": error: ") {
                line = String(line[marker.upperBound...])
            } else if line.hasPrefix(program + ": ") {
                line = String(line.dropFirst(program.count + 2))
            }
            if !line.isEmpty {
                found.append(line)
            }
        }
        return found.joined(separator: " ")
    }

    private static func standardized(_ path: String) -> String {
        let clean = (path as NSString).standardizingPath
        return clean.isEmpty ? path : clean
    }

    /// The device and inode of the folder at `path`, whose links are already followed (none of
    /// `wholeDisk` is a link); nil when it cannot be read.
    private static func fileIdentity(_ path: String) -> [UInt64]? {
        guard let attributes = try? FileManager.default.attributesOfItem(atPath: path),
              let device = attributes[.systemNumber] as? NSNumber,
              let inode = attributes[.systemFileNumber] as? NSNumber
        else {
            return nil
        }
        return [device.uint64Value, inode.uint64Value]
    }
}
