import Foundation

/// One catalog operation as `lucille-host ops list` prints it. The menu calls them abilities.
struct Ability: Equatable, Identifiable, Sendable {
    let name: String
    let state: String

    var id: String { name }

    /// What the menu calls it: a few plain words, or the catalog name if the app has none.
    var title: String { HostText.abilityTitles[name] ?? name }

    /// One plain sentence on what Lucille gets while it is on, if the app has one.
    var detail: String? { HostText.abilityDetails[name] }

    /// Whether the Abilities menu lists it. Diagnostic and always-on operations are left out, and
    /// so is a name the policy holds that this release does not have.
    var isShown: Bool { !HostText.unlistedAbilities.contains(name) && !state.hasPrefix("in the policy") }

    /// Shown with a checkmark: the local policy names it.
    var isOn: Bool { state.hasPrefix("enabled") || state.hasPrefix("in the policy") }

    /// Whether the menu may switch it: off when it is on, on when nothing stops it.
    var canChange: Bool { isOn || state == "disabled" || state.hasPrefix("disabled: an upgrade") }

    /// A plain sentence for a state that is more than on or off.
    var note: String? {
        if state == "enabled" || state == "disabled" {
            return nil
        }
        if state.hasPrefix("disabled: an upgrade") {
            return "An update changed this ability, so it was turned off."
        }
        if state.hasPrefix("enabled, but not on this computer") {
            return "On, but this Mac lacks a program it needs."
        }
        if state.hasPrefix("disabled; not on this computer") {
            return "This Mac lacks a program it needs."
        }
        if state.hasPrefix("in the policy") {
            return "Not in this version of \(HostText.appName)."
        }
        return state
    }
}

/// One folder alias as `lucille-host roots list` prints it.
struct Folder: Equatable, Identifiable, Sendable {
    let alias: String
    let path: String

    var id: String { alias }
}

/// What the app tells the user about a run that failed: one short sentence, and the host's own
/// words, if it gave any, as a detail.
struct Explanation: Equatable, Sendable {
    let message: String
    let detail: String?
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

/// Reading what the host's command line prints. It has no machine-readable output yet, so this
/// follows its text exactly (`src/lucille_host/cli.py`); the self-test checks it against the
/// real command line on every build.
enum HostText {
    private static let program = "lucille-host"

    /// The menu's words for each catalog operation it lists. Every operation in the catalog is
    /// either here or in `unlistedAbilities` (`tests/test_macos_build.py` checks that).
    static let abilityTitles: [String: String] = [
        "files.sha256": "Check a file's fingerprint",
    ]

    /// One plain sentence for each ability the menu lists: what Lucille gets while it is on.
    /// The menu shows it under the ability (`tests/test_macos_build.py` checks there is one).
    static let abilityDetails: [String: String] = [
        "files.sha256": "Lucille gets a code that identifies one file's exact contents, not the contents.",
    ]

    /// Operations the Abilities menu does not list: `host.describe`, which is on from the start
    /// and is described in the menu instead, and `host.sleep`, which is for testing.
    static let unlistedAbilities: Set<String> = ["host.describe", "host.sleep"]

    /// The app's name, from its Info.plist, where build_app.py puts the name macos/runtime.toml
    /// holds, so that a new name needs no change here. Code run outside the app, such as the
    /// self-test, finds no name there and uses "the app". That fallback is deliberately not the
    /// app's name: if the app ever stopped reading its Info.plist, the `ui` check, which looks for
    /// the name from runtime.toml, would fail instead of passing on the fallback.
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

    /// What the app says once a folder is added. Nothing can connect yet, so it says when the
    /// folder becomes of use.
    static func folderAdded(alias: String) -> String {
        "Once this Mac is connected to Lucille, Lucille can use \u{201C}\(alias)\u{201D} for the abilities you turn on."
    }

    /// What the app says when macOS's privacy protection kept the host from a folder.
    static let blockedByMacOS = "macOS is blocking \(appName) from this folder. To allow it, open System Settings, choose Privacy & Security, then Files & Folders, and turn on the switch for this folder's location under \(appName). Then add the folder again."

    /// What the app says while a run it stopped waiting for has still not ended. It may hold the
    /// host's policy lock, so every change to folders and abilities waits for it.
    static let stuckRun = "A folder check is still stuck, probably on a network or external drive that stopped answering. Until it ends, \(appName) can't change its folders or abilities. Reconnect the drive, or restart your Mac, then try again."

    /// "lucille-host 0.1.0" gives "0.1.0".
    static func version(fromVersionOutput text: String) -> String? {
        let line = text.trimmingCharacters(in: .whitespacesAndNewlines)
        let prefix = program + " "
        guard line.hasPrefix(prefix), !line.contains("\n") else {
            return nil
        }
        let version = String(line.dropFirst(prefix.count))
        return version.isEmpty || version.contains(" ") ? nil : version
    }

    /// Each "name  state" line of `ops list`.
    static func abilities(fromList text: String) -> [Ability] {
        pairs(text).map { Ability(name: $0.0, state: $0.1) }
    }

    /// Each "alias  path" line of `roots list`.
    static func folders(fromList text: String) -> [Folder] {
        pairs(text).map { Folder(alias: $0.0, path: $0.1) }
    }

    /// Whether `roots add` refused because the folder is the home folder, holds it, or is "/".
    /// The user may then confirm, unless it is the whole disk, and the app adds
    /// `--allow-home-or-root`.
    static func needsHomeConfirmation(_ result: CLIResult) -> Bool {
        result.status == 1 && !result.timedOut && !result.cancelled
            && result.stderr.contains("--allow-home-or-root")
    }

    /// The whole disk, which the app never adds, whatever the user chooses: the top of the
    /// startup disk, the Data volume that holds every user's files, and the two folders between
    /// them, which hold that volume. The command line still adds each of them with
    /// `--allow-home-or-root`; the app never passes it for them (`addFolderArguments`).
    static let wholeDisk = ["/", "/System", "/System/Volumes", "/System/Volumes/Data"]

    /// Whether `path` is the whole disk: one of `wholeDisk` as written or with its links
    /// followed, in any case, or the same folder as one of them by device and inode.
    static func isWholeDisk(path: String) -> Bool {
        let spellings = [
            standardized(path),
            standardized(URL(fileURLWithPath: path).resolvingSymlinksInPath().path),
        ]
        let named = spellings.contains { spelling in
            wholeDisk.contains { spelling.caseInsensitiveCompare($0) == .orderedSame }
        }
        if named {
            return true
        }
        guard let identity = fileIdentity(path) else {
            return false
        }
        return wholeDisk.contains { fileIdentity($0) == identity }
    }

    /// The `roots add` command line for a folder. `--allow-home-or-root` is there only when the
    /// user confirmed a home folder or a folder holding it, and never for the whole disk, whatever
    /// was confirmed.
    static func addFolderArguments(alias: String, path: String, confirmedHome: Bool) -> [String] {
        var verb = ["roots", "add", alias, path]
        if confirmedHome && !isWholeDisk(path: path) {
            verb.append("--allow-home-or-root")
        }
        return verb
    }

    /// Whether `path` is one the app asks about before it adds it, as far as it can tell from the
    /// path: the home folder, or a folder that holds it. The whole disk holds it too, but is not
    /// asked about: the app refuses it. The host decides; this lets the app ask before the user
    /// names the folder.
    static func broadFolder(path: String, home: String) -> BroadFolder? {
        if isWholeDisk(path: path) {
            return nil
        }
        let folder = standardized(path)
        let homeFolder = standardized(home)
        if folder == homeFolder {
            return .home
        }
        if homeFolder.hasPrefix(folder + "/") {
            return .holdsHome
        }
        return nil
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
        /// Adding a folder, with what the app itself can see of it.
        case addingFolder(FolderAccess)
        case other
    }

    /// What to tell the user when a run failed: a plain sentence for each refusal the host's
    /// command line gives, and its own words as the detail. `earlierRunStuck` is whether a run
    /// the app stopped waiting for has still not ended (`HostCLI.stuckRuns`).
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
        let reason = reasons(result.stderr)
        guard !reason.isEmpty else {
            return Explanation(
                message: "Something went wrong in \(appName)'s helper program.",
                detail: "It stopped with exit status \(result.status)."
            )
        }
        return Explanation(message: plain(reason, during: activity), detail: reason)
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

    /// A plain sentence for each refusal `src/lucille_host/cli.py` can give.
    private static func plain(_ reason: String, during activity: Activity) -> String {
        // The host reads a folder macOS keeps from it as no folder at all, or fails with EPERM.
        // When the app can see that folder, or is refused too, macOS is what stands in the way.
        var adding = false
        var seenByApp = false
        if case .addingFolder(let access) = activity {
            adding = true
            seenByApp = access != .missing
        }
        let taken = " is already a folder alias; remove it first"
        if reason.hasSuffix(taken) {
            let alias = String(reason.dropLast(taken.count))
            return "You already have a folder named \(alias). Choose another name."
        }
        if reason.hasPrefix("the policy holds at most ") {
            let limit = reason.dropFirst("the policy holds at most ".count).prefix { $0.isNumber }
            return "\(appName) can use at most \(limit) folders. Remove one before you add another."
        }
        if reason.hasSuffix(" is where lucille-host keeps its own files") {
            return "That folder holds \(appName)'s own settings, so it can't be added."
        }
        if reason.hasSuffix(" is not a folder") {
            return seenByApp ? blockedByMacOS : "That folder is no longer there."
        }
        if adding && reason.hasPrefix("Operation not permitted") {
            return blockedByMacOS
        }
        if reason.hasPrefix("the policy file ") && reason.contains(" is invalid: ") {
            return "\(appName)'s settings file can't be read."
        }
        if reason.hasPrefix("cannot lock ") {
            return "\(appName) couldn't lock its settings file, so nothing was changed. Try again."
        }
        if reason.hasPrefix("no folder alias ") {
            return "That folder was already removed."
        }
        if reason.hasSuffix(" is not enabled") {
            return "That ability was already off."
        }
        if reason.hasSuffix(" is not in this release's catalog") {
            return "This version of \(appName) doesn't have that ability."
        }
        if reason.contains(" cannot run here: ") {
            return "This Mac lacks a program that ability needs."
        }
        if reason.hasPrefix("an alias is ") {
            return "That name can't be used."
        }
        if reason.contains("--allow-home-or-root") {
            return "That folder is your home folder, holds it, or is your whole disk."
        }
        return "Something went wrong in \(appName)'s helper program."
    }

    private static func standardized(_ path: String) -> String {
        let clean = (path as NSString).standardizingPath
        return clean.isEmpty ? path : clean
    }

    /// A folder's device and inode, with its links followed; nil when it cannot be read.
    private static func fileIdentity(_ path: String) -> [UInt64]? {
        let real = URL(fileURLWithPath: path).resolvingSymlinksInPath().path
        guard let attributes = try? FileManager.default.attributesOfItem(atPath: real),
              let device = attributes[.systemNumber] as? NSNumber,
              let inode = attributes[.systemFileNumber] as? NSNumber
        else {
            return nil
        }
        return [device.uint64Value, inode.uint64Value]
    }

    private static func pairs(_ text: String) -> [(String, String)] {
        var found: [(String, String)] = []
        for line in text.split(separator: "\n", omittingEmptySubsequences: true) {
            guard let gap = line.range(of: "  ") else {
                continue
            }
            let head = String(line[..<gap.lowerBound])
            let tail = String(line[gap.upperBound...])
            if !head.isEmpty && !tail.isEmpty && !head.contains(" ") {
                found.append((head, tail))
            }
        }
        return found
    }
}
