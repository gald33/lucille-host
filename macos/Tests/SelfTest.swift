import Foundation

/// Runs the app's code that draws nothing (`Alias`, `HostCLI`, `HostText`, `HostJSON`, `Pairing`,
/// `Supervisor`, `ConnectionText`, `RequestLog`, `Placement`) without a screen: where a copy of
/// the app may run, the host's golden fixtures through the JSON reader, Activity's request log and
/// the two state machines, the latch that keeps a misplaced copy from starting anything, fake
/// children through the child runner, and, against a built app, the calls the menu makes, through
/// the same code, with a temporary HOME. CI compiles it with those files (`tools/macos/check_app.py
/// swift`). Exit status 1 on any failure.
///
///     selftest --vectors alias-vectors.json [--fixtures DIR] [--home DIR]
///              [--app NAME.app --home DIR --version V]
@main
struct SelfTest {
    static func main() {
        var options: [String: String] = [:]
        var args = CommandLine.arguments.dropFirst()
        while let key = args.popFirst() {
            guard key.hasPrefix("--"), let value = args.popFirst() else {
                print("usage: selftest --vectors FILE [--fixtures DIR] [--app APP --home DIR --version V]")
                exit(2)
            }
            options[String(key.dropFirst(2))] = value
        }
        let report = Report()
        if let vectors = options["vectors"] {
            checkAliases(vectorsAt: vectors, report)
        }
        checkText(report)
        checkCodeRules(report)
        checkPlacement(home: options["home"], report)
        let fixtures = options["fixtures"].map { loadFixtures(at: $0, report) } ?? [:]
        if options["app"] != nil {
            report.expect(!fixtures.isEmpty, "the host's CLI fixtures are given with --fixtures")
        }
        checkJSON(fixtures, report)
        checkRequestLog(fixtures, report)
        checkPairing(fixtures, report)
        checkSupervisor(fixtures, report)
        if let home = options["home"] {
            // First among the checks that start anything: until it opens the latch, nothing can.
            checkSpawnLatch(home: home, report)
            checkChild(home: home, report)
        }
        if let app = options["app"], let home = options["home"], let version = options["version"] {
            checkCLI(app: URL(fileURLWithPath: app), home: home, version: version, report)
        }
        print("\(report.passed) passed, \(report.failed) failed")
        exit(report.failed == 0 && report.passed > 0 ? 0 : 1)
    }

    static func checkAliases(vectorsAt path: String, _ report: Report) {
        struct Vector: Decodable {
            let folder: String
            let taken: [String]?
            let alias: String
        }
        guard let data = FileManager.default.contents(atPath: path),
              let vectors = try? JSONDecoder().decode([Vector].self, from: data)
        else {
            report.expect(false, "read the alias vectors at \(path)")
            return
        }
        report.expect(!vectors.isEmpty, "\(vectors.count) alias vectors")
        for vector in vectors {
            let taken = Set(vector.taken ?? [])
            let got = Alias.suggest(forFolderName: vector.folder, taken: taken)
            report.expect(got == vector.alias,
                          "suggest(\(vector.folder.debugDescription), taken: \(taken.sorted())) is \(vector.alias)", got)
            report.expect(Alias.isValid(got) && !taken.contains(got), "\(got) is a valid alias nobody uses")
        }
        let taken: Set<String> = ["taxes"]
        report.expect(Alias.review(typed: " notes ", suggestion: "taxes-2", taken: taken) == .use("notes"),
                      "a valid name nobody uses is taken as typed")
        if case .offer(let name, let because) = Alias.review(typed: "taxes", suggestion: "taxes-2", taken: taken) {
            report.expect(name == "taxes-2" && because == "You already have a folder named taxes. Choose another name.",
                          "a name in use is refused in plain words, with a free one offered", "\(name) \(because)")
        } else {
            report.expect(false, "a name in use is refused")
        }
        if case .offer(let name, let because) = Alias.review(typed: "My Taxes", suggestion: "taxes-2", taken: taken) {
            report.expect(name == "my-taxes" && because.contains("\u{201C}my-taxes\u{201D}"),
                          "a name the host would refuse is turned into one it accepts", "\(name) \(because)")
        } else {
            report.expect(false, "a name the host would refuse is fixed, not taken")
        }
        if case .offer(let name, _) = Alias.review(typed: "  ", suggestion: "taxes-2", taken: taken) {
            report.expect(name == "taxes-2", "an empty name brings back the suggestion", name)
        } else {
            report.expect(false, "an empty name is not taken")
        }
        for bad in ["", "A", "1a", "-a", "_a", "a b", "a.b", "é", String(repeating: "a", count: 33)] {
            report.expect(!Alias.isValid(bad), "\(bad.debugDescription) is not a valid alias")
        }
        for good in ["a", "a-b_c9", "documents", String(repeating: "a", count: 32)] {
            report.expect(Alias.isValid(good), "\(good) is a valid alias")
        }
    }

    static func checkText(_ report: Report) {
        report.expect(HostText.version(fromVersionOutput: "lucille-host 0.1.0.dev0\n") == "0.1.0.dev0",
                      "the version line parses")
        report.expect(HostText.version(fromVersionOutput: "something else 1.0\n") == nil,
                      "another program's line does not")
        let ops = HostText.abilities(fromList: """
            files.search  disabled; not on this computer: /usr/bin/rg is missing
            files.sha256  enabled
            host.describe  disabled: an upgrade changed it; `lucille-host ops enable host.describe` accepts it
            old.op  in the policy, but not in this release's catalog
            """)
        report.expect(ops.map(\.name) == ["files.search", "files.sha256", "host.describe", "old.op"],
                      "ops list lines parse", "\(ops)")
        report.expect(ops.map(\.isOn) == [false, true, false, true], "on and off read right")
        report.expect(ops.map(\.canChange) == [false, true, true, true], "which can change")
        report.expect(ops[1].note == nil && ops[0].note != nil, "notes only for unusual states")
        report.expect(ops.map(\.isShown) == [true, true, false, false],
                      "host.describe and names not in this release are not listed")
        report.expect(ops[1].title == "Check a file's fingerprint" && ops[0].title == "files.search",
                      "a listed ability has plain words, or its name when the app has none")
        report.expect(Ability(name: "host.sleep", state: "disabled").isShown == false, "host.sleep is not listed")
        report.expect(HostText.abilityTitles.keys.allSatisfy { !HostText.unlistedAbilities.contains($0) },
                      "no ability is both titled and unlisted")
        let roots = HostText.folders(fromList: "docs  /Volumes/Data/My  Documents\nwork  /Volumes/Data/work\n")
        report.expect(roots == [Folder(alias: "docs", path: "/Volumes/Data/My  Documents"),
                                Folder(alias: "work", path: "/Volumes/Data/work")],
                      "roots list lines parse, with two spaces inside a path", "\(roots)")
        let usage = CLIResult(
            status: 2, stdout: "",
            stderr: """
                usage: lucille-host roots add [-h] [--allow-home-or-root] alias path
                       more usage
                lucille-host roots add: error: the following arguments are required: path

                """,
            timedOut: false)
        report.expect(HostText.explain(usage) == Explanation(
                          message: "Something went wrong in \(HostText.appName)'s helper program.",
                          detail: "the following arguments are required: path"),
                      "a usage error is a plain sentence, with the host's words as the detail", "\(HostText.explain(usage))")
        func refusal(_ text: String) -> CLIResult {
            CLIResult(status: 1, stdout: "", stderr: "lucille-host: \(text)\n", timedOut: false)
        }
        let plain = [
            ("no folder alias 'x'", "That folder was already removed."),
            ("taxes is already a folder alias; remove it first",
             "You already have a folder named taxes. Choose another name."),
            ("the policy holds at most 64 folder aliases",
             "\(HostText.appName) can use at most 64 folders. Remove one before you add another."),
            ("/Users/a/Library/Application Support/lucille-host is where lucille-host keeps its own files",
             "That folder holds \(HostText.appName)'s own settings, so it can't be added."),
            ("the policy file /x/policy.json is invalid: ops.files.sha256.definition must be 64 lowercase hex digits",
             "\(HostText.appName)'s settings file can't be read."),
            ("cannot lock /x/.policy.lock (Operation not supported), so the policy was not changed",
             "\(HostText.appName) couldn't lock its settings file, so nothing was changed. Try again."),
            ("files.sha256 is not enabled", "That ability was already off."),
            ("/gone is not a folder", "That folder is no longer there."),
            ("something new", "Something went wrong in \(HostText.appName)'s helper program."),
        ]
        for (text, message) in plain {
            let got = HostText.explain(refusal(text))
            report.expect(got.message == message && got.detail == text,
                          "the refusal \(text.debugDescription) reads as \(message.debugDescription)", "\(got)")
        }
        // Reset Settings… is offered for a settings file that can't be read, and for nothing else
        // (decisions/0002 §14).
        let unreadable = HostText.explain(refusal("the policy file /x/policy.toml is invalid: /x/policy.toml is not valid TOML"))
        report.expect(unreadable.offersReset && unreadable.message == "\(HostText.appName)'s settings file can't be read.",
                      "a settings file that can't be read offers Reset Settings", "\(unreadable)")
        for (text, _) in plain {
            report.expect(HostText.explain(refusal(text)).offersReset == text.hasPrefix("the policy file "),
                          "the refusal \(text.debugDescription) offers Reset Settings only if it is the settings file's")
        }
        report.expect(!HostText.explain(usage).offersReset
                          && !HostText.explain(refusal("the earlier policy /x/policy.previous.toml is invalid: x")).offersReset
                          && !HostText.explain(CLIResult(status: 15, stdout: "", stderr: "", timedOut: true)).offersReset,
                      "nor does a usage error, the policy a pairing set aside, or a run that took too long")
        let late = CLIResult(status: 15, stdout: "", stderr: "", timedOut: true)
        let adding = HostText.Activity.addingFolder(.visible)
        report.expect(HostText.explain(late, during: adding).message.hasPrefix("Checking this folder took too long")
                          && HostText.explain(late, during: adding).message.contains("If macOS asked")
                          && HostText.explain(late).message != HostText.explain(late, during: adding).message
                          && !HostText.needsHomeConfirmation(late),
                      "a timeout has its own words, and says more while adding a folder")

        // macOS's privacy protection: the host sees no folder, or gets EPERM, where the app sees one.
        let notAFolder = refusal("/Users/a/Documents/Taxes is not a folder")
        report.expect(HostText.explain(notAFolder, during: .addingFolder(.visible)).message == HostText.blockedByMacOS
                          && HostText.explain(notAFolder, during: .addingFolder(.blocked)).message == HostText.blockedByMacOS,
                      "a folder the app can see, or is refused too, is blocked by macOS")
        report.expect(HostText.explain(notAFolder, during: .addingFolder(.missing)).message == "That folder is no longer there."
                          && HostText.explain(notAFolder).message == "That folder is no longer there.",
                      "a folder nobody can see is gone")
        let eperm = refusal("Operation not permitted (/Users/a/Documents)")
        report.expect(HostText.explain(eperm, during: .addingFolder(.visible)).message == HostText.blockedByMacOS
                          && HostText.explain(eperm, during: .addingFolder(.visible)).detail == "Operation not permitted (/Users/a/Documents)"
                          && HostText.explain(eperm).message != HostText.blockedByMacOS,
                      "EPERM while adding a folder is blocked by macOS, with the host's words as the detail")
        report.expect(HostText.blockedByMacOS.contains("Privacy & Security") && HostText.blockedByMacOS.contains("Files & Folders"),
                      "it says where to allow it", HostText.blockedByMacOS)

        // A run the app gave up on, which may still hold the host's policy lock.
        let leftBehind = CLIResult(status: CLIResult.notFinished, stdout: "", stderr: "", timedOut: false, cancelled: true)
        report.expect(HostText.explain(leftBehind).message == HostText.stuckRun
                          && HostText.explain(leftBehind, during: adding).message == HostText.stuckRun,
                      "a run that would not end says that it is stuck")
        let blockedLater = CLIResult(status: 15, stdout: "", stderr: "", timedOut: false, cancelled: true)
        report.expect(HostText.explain(late, earlierRunStuck: true).message == HostText.stuckRun
                          && HostText.explain(late, during: adding, earlierRunStuck: true).message == HostText.stuckRun
                          && HostText.explain(blockedLater, earlierRunStuck: true).message == HostText.stuckRun,
                      "a later run that times out or is cancelled names the stuck one, not its own folder")
        report.expect(HostText.explain(refusal("files.sha256 is not enabled"), earlierRunStuck: true).message
                          == "That ability was already off.",
                      "a refusal is still its own, whatever else is stuck")

        // Where a folder is, and what the name dialog says first.
        let home = "/Users/a"
        report.expect(HostText.place(of: "/Users/a/Documents/Taxes", home: home).protectedFolder == "Documents"
                          && HostText.place(of: "/Users/a/Desktop", home: home).protectedFolder == "Desktop"
                          && HostText.place(of: "/Users/a/Downloads/x/", home: home).protectedFolder == "Downloads"
                          && HostText.place(of: "/Users/a/Documentsx", home: home) == FolderPlace()
                          && HostText.place(of: "/Users/a/work", home: home) == FolderPlace(),
                      "Documents, Desktop and Downloads are protected; a sibling is not")
        report.expect(HostText.place(of: "/Volumes/Backup/Photos", home: home).onOtherDrive
                          && !HostText.place(of: "/Users/a/Volumes/x", home: home).onOtherDrive,
                      "a folder under /Volumes is on another drive")
        report.expect(HostText.place(of: "/Users/a/Library/Mobile Documents/com~apple~CloudDocs/x", home: home).inCloud
                          && HostText.place(of: "/Users/a/Library/CloudStorage/Drive/x", home: home).inCloud
                          && HostText.place(of: "/Users/a/Documents/x", home: home, synced: true).inCloud
                          && !HostText.place(of: "/Users/a/work", home: home).inCloud,
                      "iCloud Drive and other cloud folders are in the cloud")
        let documents = HostText.aliasPrompt(folderName: "Taxes", note: nil,
                                             place: HostText.place(of: "/Users/a/Documents/Taxes", home: home, synced: true))
        report.expect(documents.contains("access files in your Documents folder. Click Allow")
                          && documents.hasSuffix("may not work with \(HostText.appName) yet."),
                      "a folder in Documents is announced, and an iCloud one warned about", documents)
        let drive = HostText.aliasPrompt(folderName: "Photos", note: nil,
                                         place: HostText.place(of: "/Volumes/Backup/Photos", home: home))
        report.expect(drive.contains("on a removable or network volume. Click Allow"), "a folder on another drive is announced", drive)
        report.expect(!HostText.aliasPrompt(folderName: "Work", note: nil).contains("macOS"),
                      "a folder elsewhere brings no announcement")

        report.expect(HostText.statusItemName(paused: false) == HostText.appName
                          && HostText.statusItemName(paused: true) == "\(HostText.appName), paused",
                      "the menu bar item has a name, which says when Lucille is paused")
        report.expect(HostText.folderAdded(alias: "taxes").hasPrefix("Once this Mac is connected to Lucille,"),
                      "a folder added is useful once connected, and the app says so")
        report.expect(HostText.welcomeText.contains("menu bar") && HostText.welcomeText.contains("by the clock")
                          && !HostText.welcomeText.contains("\\"),
                      "the welcome says where the menu is", HostText.welcomeText)
        report.expect(HostText.welcomeText.hasPrefix(HostText.appPurpose + "\n\n")
                          && HostText.appPurpose.hasPrefix("\(HostText.appName) lets Lucille use only the folders you choose"),
                      "the welcome first says what the app is for", HostText.welcomeText)
        let detailed = Ability(name: "files.sha256", state: "disabled")
        report.expect(detailed.detail == HostText.abilityDetails["files.sha256"] && detailed.detail?.contains("not the contents") == true,
                      "an ability says what Lucille gets")
        let broken = CLIResult(status: CLIResult.notStarted, stdout: "", stderr: "no such file", timedOut: false)
        report.expect(HostText.explain(broken) == Explanation(message: "\(HostText.appName) is damaged. Download it again.",
                                                              detail: "no such file"),
                      "a helper program that cannot start says to download the app again")
        let silent = CLIResult(status: 3, stdout: "", stderr: "", timedOut: false)
        report.expect(HostText.explain(silent).message == "Something went wrong in \(HostText.appName)'s helper program."
                          && HostText.explain(silent).detail == "It stopped with exit status 3.",
                      "a failure with no reason names no status in its sentence")

        report.expect(HostText.broadFolder(path: "/Users/a", home: "/Users/a") == .home, "the home folder is broad")
        report.expect(HostText.broadFolder(path: "/Users/a/", home: "/Users/a") == .home, "with a slash too")
        report.expect(HostText.broadFolder(path: "/Users", home: "/Users/a") == .holdsHome, "a folder holding it is")
        report.expect(HostText.broadFolder(path: "/Users/a/Documents", home: "/Users/a") == nil, "a folder inside is not")
        report.expect(HostText.broadFolder(path: "/Users/ab", home: "/Users/a") == nil, "a sibling is not")
        for kind in [BroadFolder.home, .holdsHome] {
            let question = HostText.broadFolderQuestion(kind)
            report.expect(question.text.contains("Documents, Desktop and Downloads") && question.text.contains("Library"),
                          "the question for \(kind) says what is inside")
        }
        let confirmedHome = HostText.addFolderArguments(alias: "home", path: "/Users/a", confirmedHome: true)
        report.expect(confirmedHome == ["roots", "add", "home", "/Users/a", "--allow-home-or-root"],
                      "a confirmed home folder gets --allow-home-or-root", "\(confirmedHome)")
        let unconfirmedHome = HostText.addFolderArguments(alias: "home", path: "/Users/a", confirmedHome: false)
        report.expect(unconfirmedHome == ["roots", "add", "home", "/Users/a"],
                      "an unconfirmed one does not", "\(unconfirmedHome)")

        // The whole disk is refused outright: never asked about, and never given the flag.
        let wholeDisk = ["/", "/System", "/System/Volumes", "/System/Volumes/Data",
                         "/System/Volumes/Data/", "//System//Volumes/Data", "/system/volumes/DATA"]
        for disk in wholeDisk {
            report.expect(HostText.isWholeDisk(path: disk), "\(disk) is the whole disk")
            report.expect(HostText.broadFolder(path: disk, home: "/Users/a") == nil,
                          "\(disk) is not asked about, as the home folder is")
            report.expect(HostText.addFolderArguments(alias: "disk", path: disk, confirmedHome: true)
                              == ["roots", "add", "disk", disk],
                          "\(disk) never gets --allow-home-or-root, even confirmed")
        }
        for folder in ["/Users", "/Users/a", "/System/Volumes/Data/Users", "/System/Library",
                       "/Volumes/Data", "/Users/a/System/Volumes/Data", "/Systems"] {
            report.expect(!HostText.isWholeDisk(path: folder), "\(folder) is not the whole disk")
        }
        let refusal = HostText.wholeDiskRefusal(diskName: "Macintosh HD")
        report.expect(refusal.title == "\(HostText.appName) can't use your whole disk"
                          && refusal.text.hasPrefix("This folder holds every user's files on Macintosh HD, your startup disk")
                          && refusal.text.contains("Documents, Desktop and Downloads") && refusal.text.contains("Library")
                          && refusal.text.hasSuffix("Choose one folder instead."),
                      "the refusal names the disk as Finder names it, and says what is on it", "\(refusal)")
        let prompt = HostText.aliasPrompt(folderName: "Taxes", note: "Note.")
        report.expect(prompt.hasPrefix("Note.\n\nLucille will know") && prompt.contains("never learns where"),
                      "the name dialog leads with its note, then its purpose", prompt)
    }

    static func checkCLI(app: URL, home: String, version: String, _ report: Report) {
        let cli = HostCLI(python: HostCLI.python(inApp: app), home: home)
        report.expect(HostText.access(of: home) == .visible
                          && HostText.access(of: home + "/no such folder") == .missing,
                      "the app sees a folder that is there, and not one that is not")
        let env = cli.environment()
        report.expect(Set(env.keys).isSubset(of: ["HOME", "PATH", "LANG", "USER", "LOGNAME", "TMPDIR"]),
                      "the environment is fixed", "\(env.keys.sorted())")
        report.expect(cli.arguments(for: ["pause"]) == ["-I", "-B", "-X", "utf8", "-m", "lucille_host", "--profile", "mac-app", "pause"],
                      "the interpreter runs isolated, in UTF-8 mode, without writing bytecode, on the app's own profile")
        report.expect(env["PATH"] == "/var/empty", "PATH names an empty folder", "\(env["PATH"] ?? "none")")

        let shown = cli.run(["--version"])
        report.expect(shown.succeeded && HostText.version(fromVersionOutput: shown.stdout) == version,
                      "--version gives \(version)", "\(shown)")

        let ops = cli.run(["ops", "list"])
        let abilities = HostText.abilities(fromList: ops.stdout)
        report.expect(ops.succeeded && !abilities.isEmpty, "ops list gives \(abilities.count) abilities",
                      "\(ops)")
        if let off = abilities.first(where: { $0.state == "disabled" }) {
            let on = cli.run(["ops", "enable", off.name])
            let after = HostText.abilities(fromList: cli.run(["ops", "list"]).stdout)
            report.expect(on.succeeded && after.first(where: { $0.name == off.name })?.isOn == true,
                          "turning \(off.name) on shows a checkmark", "\(on)")
            let back = cli.run(["ops", "disable", off.name])
            let again = HostText.abilities(fromList: cli.run(["ops", "list"]).stdout)
            report.expect(back.succeeded && again.first(where: { $0.name == off.name })?.isOn == false,
                          "turning it off again removes it", "\(back)")
        } else {
            print("note: no ability is plainly off here, so the switch round trip was skipped")
        }

        // Whether the host is paused, from the controls object each writes, never from status,
        // which opens the Keychain.
        let paused = VerbOutcome(cli.run(["--json", "pause"]))
        report.expect(paused.paused == true, "pause --json says the host is paused", "\(paused)")
        let resumed = VerbOutcome(cli.run(["--json", "resume"]))
        report.expect(resumed.paused == false, "resume --json says it is not", "\(resumed)")
        let pausedText = cli.run(["pause"])
        let resumedText = cli.run(["resume"])
        report.expect(pausedText.succeeded && resumedText.succeeded, "pause and resume work as the menu runs them",
                      "\(pausedText) \(resumedText)")

        let folder = URL(fileURLWithPath: home).appendingPathComponent("My Project Files")
        try? FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        let alias = Alias.suggest(forFolderName: folder.lastPathComponent)
        report.expect(alias == "my-project-files", "the folder's suggested alias", alias)
        let added = cli.run(["roots", "add", alias, folder.path])
        report.expect(added.succeeded, "roots add \(alias)", "\(added)")
        let listed = HostText.folders(fromList: cli.run(["roots", "list"]).stdout)
        report.expect(listed == [Folder(alias: alias, path: folder.path)], "the folder is listed", "\(listed)")
        let removed = cli.run(["roots", "remove", alias])
        report.expect(removed.succeeded && HostText.folders(fromList: cli.run(["roots", "list"]).stdout).isEmpty,
                      "roots remove \(alias)", "\(removed)")
        let missing = cli.run(["roots", "remove", alias])
        report.expect(!missing.succeeded && HostText.explain(missing).message == "That folder was already removed.",
                      "removing it twice is refused in plain words", "\(HostText.explain(missing))")

        report.expect(cli.run(["roots", "add", alias, folder.path]).succeeded, "roots add \(alias) again")
        let twice = cli.run(["roots", "add", alias, folder.path])
        report.expect(!twice.succeeded
                          && HostText.explain(twice).message == "You already have a folder named \(alias). Choose another name.",
                      "a name in use is refused in plain words", "\(HostText.explain(twice))")
        report.expect(cli.run(["roots", "remove", alias]).succeeded, "roots remove \(alias) again")
        let own = URL(fileURLWithPath: home).appendingPathComponent("Library/Application Support/lucille-host")
        let ownFolder = cli.run(["roots", "add", "own", own.path])
        report.expect(!ownFolder.succeeded
                          && HostText.explain(ownFolder).message == "That folder holds \(HostText.appName)'s own settings, so it can't be added.",
                      "the host's own folder is refused in plain words", "\(HostText.explain(ownFolder))")
        let off = cli.run(["ops", "disable", "files.sha256"])
        report.expect(!off.succeeded && HostText.explain(off).message == "That ability was already off.",
                      "turning off an ability that is off is refused in plain words", "\(HostText.explain(off))")

        let broad = cli.run(HostText.addFolderArguments(alias: "home", path: home, confirmedHome: false))
        report.expect(HostText.needsHomeConfirmation(broad), "the home folder needs confirmation", "\(broad)")
        let confirmed = cli.run(HostText.addFolderArguments(alias: "home", path: home, confirmedHome: true))
        report.expect(confirmed.succeeded, "and is added once confirmed", "\(confirmed)")
        report.expect(cli.run(["roots", "remove", "home"]).succeeded, "and removed again")

        // The whole disk, and a link to it: the command line the app builds, even confirmed, is
        // refused by the host, so nothing is added.
        report.expect(!HostText.isWholeDisk(path: home) && !HostText.isWholeDisk(path: folder.path),
                      "the home folder and a folder in it are not the whole disk")
        for (name, target) in [("Whole Disk", "/"), ("Data Volume", "/System/Volumes/Data")] {
            let link = URL(fileURLWithPath: home).appendingPathComponent(name).path
            try? FileManager.default.createSymbolicLink(atPath: link, withDestinationPath: target)
            report.expect(HostText.isWholeDisk(path: link), "a link to \(target) is the whole disk too")
            for path in [target, link] {
                let refused = cli.run(HostText.addFolderArguments(alias: "disk", path: path, confirmedHome: true))
                report.expect(HostText.needsHomeConfirmation(refused)
                                  && HostText.folders(fromList: cli.run(["roots", "list"]).stdout).isEmpty,
                              "the host refuses \(path) as the app asks to add it, and nothing is added", "\(refused)")
            }
            try? FileManager.default.removeItem(atPath: link)
        }

        let slow = HostCLI(python: cli.python, home: home, timeout: 0.001).run(["ops", "list"])
        report.expect(slow.timedOut && !slow.succeeded, "a run past its time limit is stopped", "\(slow)")
        let short = cli.run(["ops", "list"], timeout: 0.001)
        report.expect(short.timedOut && !short.succeeded, "one run can have its own time limit", "\(short)")
        let long = HostCLI(python: cli.python, home: home, timeout: 0.001).run(["ops", "list"], timeout: 60)
        report.expect(long.succeeded, "a longer one too", "\(long)")
        let token = CancelToken()
        token.cancel()
        let started = Date()
        let cancelled = cli.run(["ops", "list"], cancel: token)
        report.expect(cancelled.cancelled && !cancelled.succeeded && !cancelled.timedOut
                          && Date().timeIntervalSince(started) < 5,
                      "a cancelled run is stopped at once", "\(cancelled)")
        let absent = HostCLI(python: URL(fileURLWithPath: "/nonexistent/python3"), home: home).run(["--version"])
        report.expect(absent.status == CLIResult.notStarted && !absent.stderr.isEmpty,
                      "a missing interpreter is reported, not a crash", "\(absent)")

        // Reset Settings, against the real host: a settings file it cannot read is refused in
        // words that offer Reset Settings…; policy reset sets it aside as policy.invalid.toml and
        // puts the fresh policy in its place; a second reset finds nothing to set aside. The
        // self-test writes the host's file here, under its temporary HOME, as the ui check does;
        // the app never does.
        let profile = URL(fileURLWithPath: home)
            .appendingPathComponent("Library/Application Support/lucille-host/\(HostCLI.appProfile)")
        let settingsFile = profile.appendingPathComponent("policy.toml").path
        let setAside = profile.appendingPathComponent("policy.invalid.toml").path
        let notToml = Data("this is not toml\n".utf8)
        try? FileManager.default.createDirectory(at: profile, withIntermediateDirectories: true)
        report.expect(FileManager.default.createFile(atPath: settingsFile, contents: notToml),
                      "a settings file the host cannot read is written")
        let unreadable = cli.run(["ops", "list"])
        report.expect(!unreadable.succeeded && HostText.explain(unreadable).offersReset
                          && HostText.explain(unreadable).message == "\(HostText.appName)'s settings file can't be read.",
                      "ops list refuses it, in words that offer Reset Settings", "\(HostText.explain(unreadable))")
        let reset = ResetOutcome(VerbOutcome(cli.run(["--json", "policy", "reset"])))
        report.expect(reset == .reset && FileManager.default.contents(atPath: setAside) == notToml,
                      "policy reset --json sets the file aside as policy.invalid.toml", "\(reset)")
        let afresh = HostText.abilities(fromList: cli.run(["ops", "list"]).stdout)
        report.expect(afresh.filter(\.isOn).map(\.name) == ["host.describe"]
                          && HostText.folders(fromList: cli.run(["roots", "list"]).stdout).isEmpty,
                      "the fresh policy has only host.describe on, and no folder", "\(afresh)")
        let again = ResetOutcome(VerbOutcome(cli.run(["--json", "policy", "reset"])))
        report.expect(again == .nothingToReset && FileManager.default.contents(atPath: setAside) == notToml,
                      "a second reset finds nothing to set aside, which is done, and changes nothing", "\(again)")
        checkPairingVerbs(python: cli.python, home: home, report)
        report.expect(HostCLI.stuckRuns == 0, "every run ended, so none is counted as stuck")
    }
}

final class Report {
    private(set) var passed = 0
    private(set) var failed = 0

    func expect(_ ok: Bool, _ what: String, _ detail: String = "") {
        if ok {
            passed += 1
            print("ok - \(what)")
        } else {
            failed += 1
            print("FAIL - \(what)" + (detail.isEmpty ? "" : ": \(detail)"))
        }
    }
}

// MARK: - Fixtures

/// One golden fixture of the host's command line (spec/CLI.md §11).
struct Fixture: Decodable {
    let description: String
    let argv: [String]
    let exit: Int32
    let stdout: [JSONValue]

    /// Each object as the line the host writes.
    var lines: [Data] {
        stdout.compactMap { try? JSONEncoder().encode($0) }
    }

    /// Each object read back through the app's reader.
    var objects: [HostObject] {
        lines.compactMap { (line: Data) -> HostObject? in
            if case .object(let object) = HostObject.parse(line: line) {
                return object
            }
            return nil
        }
    }

    var stopped: StopReason? {
        for object in objects.reversed() {
            if case .stopped(let reason) = object {
                return reason
            }
        }
        return nil
    }
}

/// A seeded generator, so that every run of the self-test walks the same sequences.
struct Seeded {
    var state: UInt64

    mutating func next(_ bound: Int) -> Int {
        state = state &* 6_364_136_223_846_793_005 &+ 1_442_695_040_888_963_407
        return Int((state >> 33) % UInt64(bound))
    }

    mutating func pick<T>(_ items: [T]) -> T {
        items[next(items.count)]
    }
}

/// What a fake child delivered.
final class ChildWatch: @unchecked Sendable {
    let queue = DispatchQueue(label: "selftest.child")
    private let lock = NSLock()
    private var gotObjects: [HostObject] = []
    private var gotNotes: [String] = []
    private var gotStderr: [String] = []
    private var gotExit: ChildExit?
    private let ended = DispatchSemaphore(value: 0)

    func watch(_ child: HostChild) {
        child.onObject = { object in
            self.lock.lock()
            self.gotObjects.append(object)
            self.lock.unlock()
        }
        child.onUnusableLine = { note in
            self.lock.lock()
            self.gotNotes.append(note)
            self.lock.unlock()
        }
        child.onStderrLine = { line in
            self.lock.lock()
            self.gotStderr.append(line)
            self.lock.unlock()
        }
        child.onExit = { finished in
            self.lock.lock()
            self.gotExit = finished
            self.lock.unlock()
            self.ended.signal()
        }
    }

    func waitForExit(seconds: Double) -> ChildExit? {
        _ = ended.wait(timeout: .now() + seconds)
        lock.lock()
        defer { lock.unlock() }
        return gotExit
    }

    var objects: [HostObject] {
        lock.lock()
        defer { lock.unlock() }
        return gotObjects
    }

    var notes: [String] {
        lock.lock()
        defer { lock.unlock() }
        return gotNotes
    }

    var stderr: [String] {
        lock.lock()
        defer { lock.unlock() }
        return gotStderr
    }

    /// Wait until `condition` holds, for `seconds` at most.
    func wait(seconds: Double, until condition: () -> Bool) -> Bool {
        let end = Date().addingTimeInterval(seconds)
        while Date() < end {
            if condition() {
                return true
            }
            Thread.sleep(forTimeInterval: 0.05)
        }
        return condition()
    }
}

extension SelfTest {
    /// 2026-11-01T12:00:00Z, the fixtures' sample time.
    static let sampleTime = Date(timeIntervalSince1970: 1_793_534_400)
    static let pairName = "pair-abcdef"

    static func loadFixtures(at folder: String, _ report: Report) -> [String: Fixture] {
        let names = (try? FileManager.default.contentsOfDirectory(atPath: folder)) ?? []
        var found: [String: Fixture] = [:]
        for name in names.sorted() where name.hasSuffix(".json") {
            guard let data = FileManager.default.contents(atPath: folder + "/" + name),
                  let fixture = try? JSONDecoder().decode(Fixture.self, from: data)
            else {
                report.expect(false, "read the fixture \(name)")
                continue
            }
            found[String(name.dropLast(5))] = fixture
        }
        report.expect(found.count >= 50, "\(found.count) of the host's CLI fixtures read from \(folder)")
        return found
    }

    static func status(
        _ pair: StatusPair, run: StatusRun = .noRecord, paused: Bool = false
    ) -> HostStatus {
        HostStatus(
            hostVersion: "0.1.0", paused: paused, policyOK: true, folders: [], enabledOps: ["host.describe"],
            pair: pair, run: run, lastHubContact: nil, lastLeftBecause: nil, clock: .unknown
        )
    }

    static func line(_ text: String) -> JSONLines.Item {
        HostObject.parse(line: Data(text.utf8))
    }

    // MARK: - The code check and the words

    static func checkCodeRules(_ report: Report) {
        let body = String(repeating: "A", count: 100)
        report.expect(Pairing.looksLikeCode("swbp1_" + body), "a code with 100 characters after its prefix looks like one")
        report.expect(!Pairing.looksLikeCode("swbp1_" + String(body.dropLast())), "99 do not")
        let longest = "swbp1_" + String(repeating: "a-_9", count: 2046) + "ab"
        report.expect(Pairing.looksLikeCode(longest), "8186 do: the whole code is then 8192 characters")
        report.expect(Data((longest + "\n").utf8).count == HostCLI.maxInputBytes,
                      "the longest code that looks like one, with its newline, is the most the child's input may be")
        report.expect(!Pairing.looksLikeCode("swbp1_" + String(repeating: "a", count: 8187)), "8187 do not")
        report.expect(!Pairing.looksLikeCode("swb1_" + body) && !Pairing.looksLikeCode("swbsecret1_" + body),
                      "an invite or a pair's secret is not a code")
        report.expect(!Pairing.looksLikeCode("swbp1_" + body + "+") && !Pairing.looksLikeCode("swbp1_" + body + "="),
                      "a character base64url does not use is refused")
        report.expect(Pairing.cleaned("  swbp1_AB\nCD \r\n") == "swbp1_ABCD", "spaces and line breaks are taken out")
        report.expect(Pairing.hint(for: "") == nil && Pairing.hint(for: " swbp1_" + body + "\n") == nil
                          && Pairing.hint(for: "hello") == ConnectionText.codeHint,
                      "the hint shows only for text that is not a code")

        let refusals = ConnectionText.refusals
        report.expect(refusals.values.allSatisfy { !$0.isEmpty }, "every refusal has words")
        let expiredBefore = ConnectionText.refusal(code: "expired", message: nil, afterSAS: false)
        let expiredAfter = ConnectionText.refusal(code: "expired", message: nil, afterSAS: true)
        let leaked = ConnectionText.refusal(code: "leaked", message: nil, afterSAS: true)
        report.expect(expiredBefore != expiredAfter && expiredBefore != leaked && expiredAfter != leaked
                          && expiredAfter == ConnectionText.expiredAfterSAS,
                      "the code expiring, the time to compare running out, and a leaked code read apart")
        report.expect(ConnectionText.refusal(code: "brand_new", message: "the host's words", afterSAS: false) == "the host's words",
                      "a code this release does not know shows the host's own sentence")
        for code in ConnectionText.appBugCodes {
            report.expect(ConnectionText.refusal(code: code, message: "x", afterSAS: false) == ConnectionText.appBug,
                          "\(code) reads as the app's own mistake")
        }
        report.expect(ConnectionText.appBugCodes.isDisjoint(with: Set(refusals.keys))
                          && Pairing.redrawCodes.isDisjoint(with: Set(refusals.keys)),
                      "no code has two sentences")
        let note = ConnectionText.note(.refused(code: "bad_code", message: "this is not a pairing code", afterSAS: false))
        report.expect(note.title == ConnectionText.couldNotConnect && note.text == refusals["bad_code"]
                          && note.detail == "this is not a pairing code",
                      "a refusal note has the app's sentence, and the host's words as details", "\(note)")
        let differed = ConnectionText.note(.rejected(lucilleTold: true, afterDiffer: true))
        let stopped = ConnectionText.note(.rejected(lucilleTold: true, afterDiffer: false))
        report.expect(differed.text.contains("intercepted") && !stopped.text.contains("intercepted")
                          && differed.title == ConnectionText.nothingConnectedTitle,
                      "They Differ says why numbers can differ; Stop Connecting does not")
        report.expect(ConnectionText.note(.rejected(lucilleTold: false, afterDiffer: true)).text.contains("remove this computer"),
                      "when Lucille was not told, the note says to remove the computer there")
        report.expect(ConnectionText.statusLine(.connected, paused: true) == "Connected to Lucille · Paused"
                          && ConnectionText.statusLine(.notConnected(nil), paused: false) == "Not connected to Lucille",
                      "the status line, and its paused suffix")
        report.expect(ConnectionText.clock(skew: 600).contains("10 minutes behind")
                          && ConnectionText.clock(skew: -360).contains("6 minutes ahead")
                          && ConnectionText.clock(skew: nil).contains("more than 5 minutes off"),
                      "the clock's problem says which way, and how far, when the host knows")
        report.expect(ConnectionText.stopNow.hasPrefix("Stop now (") && ConnectionText.stopNow.hasSuffix(" will be paused when you next open it)"),
                      "the Stop now button says what it does", ConnectionText.stopNow)
        report.expect(ConnectionText.disconnected(.refused(HostError(code: "none", message: "m", kind: nil))).text
                          == ConnectionText.wasNotConnected
                          && ConnectionText.disconnected(.done([.forget(.forgotten(name: pairName, lucilleTold: false))])).text
                          == ConnectionText.disconnectedNotTold,
                      "Disconnect takes no pair as done, and says when Lucille was not told")

        // Reset Settings (decisions/0002 §14): the question, then what the app says once
        // `policy reset --json` has run. The app never says the old file was deleted: the reset
        // sets it aside.
        report.expect(ConnectionText.resetQuestionTitle == "Reset \(HostText.appName)'s settings?"
                          && ConnectionText.resetQuestionText.contains("set its settings file aside")
                          && ConnectionText.resetQuestionText.contains("turned away"),
                      "the question says the file is set aside, and that Lucille is turned away until folders and abilities are added again",
                      ConnectionText.resetQuestionText)
        let resetDone = ConnectionText.reset(.reset)
        report.expect(resetDone.title == ConnectionText.resetTitle && resetDone.title == "Settings reset"
                          && resetDone.text.contains("set aside") && !resetDone.text.lowercased().contains("delet")
                          && resetDone.detail == nil,
                      "a reset says the old file was set aside, not deleted", "\(resetDone)")
        let resetNothing = ConnectionText.reset(.nothingToReset)
        report.expect(resetNothing.title == ConnectionText.nothingToResetTitle && resetNothing.text == refusals["not_invalid"]
                          && resetNothing.detail == nil,
                      "a reset with nothing to set aside is done, and says nothing was changed", "\(resetNothing)")
        let lockMessage = "cannot lock /x/.policy.lock (Operation not supported), so the policy was not changed"
        let resetLocked = ConnectionText.reset(.failed(.refused(HostError(code: "policy_lock_failed", message: lockMessage, kind: nil))))
        report.expect(resetLocked.title == ConnectionText.couldNotReset && resetLocked.text.contains("Try again")
                          && !resetLocked.text.contains("connected") && resetLocked.detail == lockMessage,
                      "a lock that could not be taken says to try again, not that nothing was connected", "\(resetLocked)")
        let resetLate = ConnectionText.reset(.failed(.timedOut))
        report.expect(resetLate.title == ConnectionText.couldNotReset && resetLate.text != resetLocked.text
                          && ConnectionText.reset(.failed(.timedOut), earlierRunStuck: true).text == HostText.stuckRun
                          && ConnectionText.reset(.failed(.notFinished)).text == HostText.stuckRun,
                      "a reset that did not finish says so, and names a run that is stuck", "\(resetLate)")
        report.expect(ConnectionText.reset(.failed(.notStarted)).text == ConnectionText.damaged,
                      "a helper program that cannot start says to download the app again")
        report.expect(resetLate.text == ConnectionText.resetUnknown
                          && ConnectionText.resetUnknown.contains("couldn't tell whether its settings were reset"),
                      "a reset that timed out says the app can't tell whether it worked", "\(resetLate)")
        let unknown: [VerbOutcome] = [.done([]), .signaled, .otherExit(3), .cancelled]
        for verb in unknown {
            let note = ConnectionText.reset(.failed(verb))
            report.expect(note.title == ConnectionText.couldNotReset && note.text == ConnectionText.resetUnknown
                              && note.detail == nil,
                          "a reset that ended with no policy_reset and no refusal says the app can't tell", "\(verb) \(note)")
        }
        let otherRefusal = ConnectionText.reset(.failed(.refused(HostError(code: "bad_profile", message: "m", kind: nil))))
        report.expect(otherRefusal.title == ConnectionText.couldNotReset && otherRefusal.text == ConnectionText.appBug
                          && otherRefusal.detail == "m",
                      "another refusal reads as the helper program's fault, with the host's words", "\(otherRefusal)")
        let failures: [VerbOutcome] = [
            .done([]), .signaled, .otherExit(3), .cancelled, .timedOut, .notStarted, .notFinished,
            .refused(HostError(code: "bad_profile", message: "m", kind: nil)),
            .refused(HostError(code: "policy_lock_failed", message: lockMessage, kind: nil)),
        ]
        for verb in failures {
            for stuck in [false, true] {
                let note = ConnectionText.reset(.failed(verb), earlierRunStuck: stuck)
                report.expect(note.title == ConnectionText.couldNotReset && note.text != ConnectionText.resetText
                                  && note.text != refusals["not_invalid"],
                              "a reset that failed never says the settings were reset", "\(verb) \(stuck) \(note)")
            }
        }

        // The update notice (decisions/0002 §16): the version Lucille asks for in its title; in its
        // words, the version this Mac has, where to get the newer one, and Prepare to Update, which
        // finishes the current task and quits. There is no Download button, and the words name none.
        report.expect(ConnectionText.hostTooOldTitle(minVersion: "2.0.0") == "Lucille needs \(HostText.appName) 2.0.0 or newer",
                      "the update notice names the version Lucille needs", ConnectionText.hostTooOldTitle(minVersion: "2.0.0"))
        let update = ConnectionText.hostTooOldText(hostVersion: "1.0.0")
        report.expect(update.hasPrefix("This Mac has \(HostText.appName) 1.0.0. ")
                          && update.contains("Get the newer \(HostText.appName) from where you got this one.")
                          && update.contains("click \(ConnectionText.prepareToUpdateButton).")
                          && update.contains("choose Replace.") && !update.contains("Download"),
                      "it says where to get the newer one, then to click Prepare to Update; it offers no Download", update)
        report.expect(ConnectionText.prepareToUpdateItem == ConnectionText.prepareToUpdateButton + "\u{2026}"
                          && ConnectionText.notNowButton == "Not Now",
                      "the menu's item is the note's button, with an ellipsis; the note's other button is Not Now")
    }

    // MARK: - checkPlacement

    /// Only the copy at exactly /Applications/<name>.app acts (decisions/0002 §3.3 and §13), here
    /// with a made-up name. The case of each letter counts; a copy Finder's Keep Both named
    /// "<name> 2" is renamed, with the original there or not; a disk image, a folder inside
    /// Applications, the Applications folder in the home folder, Downloads and an App
    /// Translocation folder are each refused with where they are; a link at the right path to a
    /// copy elsewhere is refused for where that copy is; a bundle with no name is never placed.
    /// Every refusal has words, and none tells a user with one copy to put it in the Trash. With a
    /// HOME, whether the original is there is read from the disk: another copy of the app at that
    /// name is one; the running copy itself is not, whether it is reached through a link or, on a
    /// disk that ignores case, spelled in another case; nor is a folder with no Info.plist, another
    /// app, or anything while the running copy's identifier is unknown.
    static func checkPlacement(home: String?, _ report: Report) {
        let user = "/Users/someone"
        func judge(_ path: String, resolved: String? = nil, name: String? = "Example", original: Bool = false) -> Placement {
            Placement.judge(bundlePath: path, resolvedPath: resolved ?? path, appName: name, home: user,
                            originalPresent: original)
        }
        report.expect(judge("/Applications/Example.app").isPlaced, "/Applications/Example.app is placed")
        report.expect(judge("/Applications/Example.app/").isPlaced && judge("/Applications/./Example.app").isPlaced,
                      "however its path is spelled")
        var refusals: [Placement] = []
        for original in [false, true] {
            for (path, copy) in [("/Applications/example.app", "example"), ("/Applications/Example 2.app", "Example 2"),
                                 ("/Applications/EXAMPLE.app", "EXAMPLE")] {
                let found = judge(path, original: original)
                report.expect(found == .renamed(copy: copy, originalPresent: original),
                              "\(path) is renamed, as \(copy), the original \(original ? "there" : "not there")", "\(found)")
                refusals.append(found)
            }
        }
        // The right path, reached through a link that leads to the same name in another case, is
        // judged by where the link leads: a renamed copy.
        let otherCase = judge("/Applications/Example.app", resolved: "/Applications/example.app")
        report.expect(otherCase == .renamed(copy: "example", originalPresent: false),
                      "/Applications/Example.app, which is /Applications/example.app, is renamed, as example", "\(otherCase)")
        refusals.append(otherCase)
        let homeCopy = user + "/Applications/Example.app"
        let homeDeeper = user + "/Applications/Tools/Example.app"
        let downloaded = user + "/Downloads/Example.app"
        let onDesktop = user + "/Desktop/Example.app"
        let places: [(String, String?, Placement.Where)] = [
            ("/Applications/Utilities/Example.app", nil, .insideApplications),
            ("/Applications/Example/Example.app", nil, .insideApplications),
            (homeCopy, nil, .homeApplications),
            (homeDeeper, nil, .homeApplications),
            ("/Volumes/Example/Example.app", nil, .diskImage),
            (downloaded, nil, .elsewhere),
            (onDesktop, nil, .elsewhere),
            ("/private/var/folders/xy/abc123/T/AppTranslocation/0A1B2C3D-4E5F/d/Example.app", nil, .elsewhere),
            ("/Example.app", nil, .elsewhere),
            ("/Applications/Example.app", downloaded, .elsewhere),
            ("/Applications/Example.app", "/Volumes/Example/Example.app", .diskImage),
            (downloaded, "/Applications/Example.app", .elsewhere),
        ]
        for (path, resolved, place) in places {
            let found = judge(path, resolved: resolved)
            report.expect(found == .notInApplications(place),
                          "\(path), which is \(resolved ?? path), is refused as \(place)", "\(found)")
            refusals.append(found)
        }
        for name in [nil, ""] as [String?] {
            for path in ["/Applications/Example.app", "/Applications/.app"] {
                let found = judge(path, name: name)
                report.expect(!found.isPlaced, "with no name, \(path) is never placed", "\(found)")
                refusals.append(found)
            }
        }
        // The copy's name comes from its folder: cut to 40 characters, without characters that
        // would turn the words around or hide part of them.
        if case .renamed(let copy, _) = judge("/Applications/" + String(repeating: "x", count: 100) + ".app") {
            report.expect(copy.count == 40, "a long name is cut to 40 characters", copy)
        } else {
            report.expect(false, "a long name in /Applications is a renamed copy")
        }
        if case .renamed(let copy, _) = judge("/Applications/Example\u{202E}2\u{200B}.app") {
            report.expect(copy == "Example2", "a name's control and format characters are left out", copy.debugDescription)
        } else {
            report.expect(false, "a name with format characters in /Applications is a renamed copy")
        }

        // The words: none for the placed copy, a title and a text for every refusal.
        report.expect(ConnectionText.placement(judge("/Applications/Example.app")) == nil, "the placed copy shows no note")
        for refusal in refusals {
            let note = ConnectionText.placement(refusal)
            report.expect(note.map { !$0.title.isEmpty && !$0.text.isEmpty } == true, "\(refusal) has words", "\(String(describing: note))")
        }
        for place in [Placement.Where.diskImage, .insideApplications, .homeApplications, .elsewhere] {
            let note = ConnectionText.placement(.notInApplications(place))
            report.expect(note?.title == ConnectionText.notInApplicationsTitle
                              && note?.text.hasSuffix(ConnectionText.movingSteps) == true,
                          "\(place): the copy needs to be in Applications, and the note says how to get it there", "\(String(describing: note))")
        }
        let alone = ConnectionText.placement(.renamed(copy: "Example 2", originalPresent: false))
        let both = ConnectionText.placement(.renamed(copy: "Example 2", originalPresent: true))
        report.expect(alone?.title == "This copy is named Example 2, so it can't run" && alone?.title == both?.title,
                      "a renamed copy says its name", "\(String(describing: alone))")
        report.expect(alone?.text.contains("Trash") == false && alone?.text.contains("rename") == true
                          && both?.text.contains("Example 2 to the Trash") == true,
                      "a copy alone is renamed, never put in the Trash; with the original there, it is the one moved to the Trash",
                      "\(String(describing: alone)) \(String(describing: both))")

        // Whether the original is there, from the disk: a folder whose Info.plist gives this copy's
        // bundle identifier.
        guard let home else {
            return
        }
        let folder = home + "/placement"
        let files = FileManager.default
        let identifier = "org.example.app"
        for name in ["Example.app", "Example 2.app", "Empty.app", "Other.app"] {
            try? files.createDirectory(atPath: folder + "/" + name + "/Contents", withIntermediateDirectories: true)
        }
        for (name, given) in [("Example.app", identifier), ("Example 2.app", identifier), ("Other.app", "org.example.other")] {
            let plist = "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<plist version=\"1.0\"><dict><key>CFBundleIdentifier</key><string>"
                + given + "</string></dict></plist>\n"
            _ = files.createFile(atPath: folder + "/" + name + "/Contents/Info.plist", contents: Data(plist.utf8))
        }
        try? files.createSymbolicLink(atPath: folder + "/Linked.app", withDestinationPath: folder + "/Example 2.app")
        func present(_ name: String, identifier given: String?) -> Bool {
            Placement.originalPresent(at: folder + "/" + name, runningAt: folder + "/Example 2.app", identifier: given)
        }
        report.expect(present("Example.app", identifier: identifier), "another copy of this app at the app's name is the original")
        report.expect(!present("Example 2.app", identifier: identifier) && !present("Linked.app", identifier: identifier)
                          && !present("Nothing.app", identifier: identifier),
                      "the running copy is not, nor a link to it, nor nothing")
        report.expect(!present("Empty.app", identifier: identifier) && !present("Other.app", identifier: identifier)
                          && !present("Example.app", identifier: nil) && !present("Example.app", identifier: ""),
                      "nor a folder with no Info.plist, nor another app, nor any copy while this one's identifier is unknown")
        if files.fileExists(atPath: folder + "/EXAMPLE.app") {
            report.expect(!Placement.originalPresent(at: folder + "/Example.app", runningAt: folder + "/example.app", identifier: identifier),
                          "on a disk that ignores case, the running copy spelled in another case is not the original")
        } else {
            print("note: this disk tells case apart, so the running copy was not looked for in another case")
        }
    }

    // MARK: - checkJSON

    static func checkJSON(_ fixtures: [String: Fixture], _ report: Report) {
        for (name, fixture) in fixtures.sorted(by: { $0.key < $1.key }) {
            var wrong: [String] = []
            for line in fixture.lines {
                let item = HostObject.parse(line: line)
                guard case .object(let object) = item else {
                    wrong.append("\(item)")
                    continue
                }
                if case .unknown(let type) = object, HostObject.knownTypes.contains(type) {
                    wrong.append("\(type) read as unknown")
                }
            }
            report.expect(wrong.isEmpty && fixture.lines.count == fixture.stdout.count,
                          "\(name): every object reads, and every type the app knows decodes", "\(wrong)")
        }
        if !fixtures.isEmpty {
            checkFixtureObjects(fixtures, report)
        }

        // Framing (spec/CLI.md §4.1 and §13.2).
        let ready = #"{"v":1,"type":"ready","host_version":"1.0.0","pair":"p"}"#
        func padded(_ total: Int) -> String {
            let head = #"{"v":1,"type":"pad","x":""#
            let tail = #""}"#
            return head + String(repeating: "a", count: total - head.utf8.count - tail.utf8.count) + tail
        }
        var lines = JSONLines()
        let atBound = lines.feed(Data((padded(JSONLines.maxLineBytes - 1) + "\n").utf8))
        report.expect(atBound == [.object(.unknown(type: "pad"))], "a line of 16384 bytes, its newline included, is read")
        let overBound = lines.feed(Data((padded(JSONLines.maxLineBytes) + "\n" + ready + "\n").utf8))
        report.expect(overBound.count == 2 && overBound.last == .object(.ready(pair: "p", hostVersion: "1.0.0")),
                      "one byte more is dropped, and the next line is read", "\(overBound)")
        if case .unusable? = overBound.first {
        } else {
            report.expect(false, "a line over the bound is a note", "\(overBound)")
        }
        let longStatus = #"{"v":1,"type":"status","host_version":"1","policy":{"state":"ok","file":null,"enabled":[],"disabled":[],"folders":["#
            + Array(repeating: #""folder-with-a-long-alias-name-xx""#, count: 600).joined(separator: ",")
            + #"]},"paused":false,"store":"keyring","hub":"h","loopback_hub":null,"pair":{"state":"none"},"work_rooms":[],"last_left_because":null,"offers_waiting":0,"run":{"state":"none"},"last_hub_contact":null,"clock":{"state":"unknown"}}"#
        let wide = HostJSON.objects(inOutput: longStatus + "\n")
        var wideFolders = 0
        if case .status(let found)? = wide.first {
            wideFolders = found.folders.count
        }
        report.expect(longStatus.utf8.count > JSONLines.maxLineBytes && wideFolders == 600,
                      "a short verb's output is read whole: run's line bound does not apply to it")
        let long = lines.feed(Data((String(repeating: "x", count: 20000) + "\n").utf8))
        report.expect(long.count == 1 && long.first != nil && !isObject(long[0]), "a 20000-byte line is dropped")
        report.expect(lines.feed(Data("not json\n\n[1,2]\n".utf8)).allSatisfy { !isObject($0) },
                      "a line that is not one JSON object is dropped")
        report.expect(lines.feed(Data((ready + "\r\n").utf8)).allSatisfy { !isObject($0) },
                      "a line that ends with a carriage return is dropped, though JSON would allow it")
        var bytewise: [JSONLines.Item] = []
        for byte in Data((ready + "\n").utf8) {
            bytewise += lines.feed(Data([byte]))
        }
        report.expect(bytewise == [.object(.ready(pair: "p", hostVersion: "1.0.0"))], "a line that arrives a byte at a time is read whole")
        report.expect(lines.feed(Data(ready.utf8)).isEmpty, "a line with no newline yet waits")
        let fragment = lines.finish()
        report.expect(fragment.count == 1 && !isObject(fragment[0]) && lines.finish().isEmpty,
                      "a last fragment with no newline is dropped, with one note")
        report.expect(line(#"{"v":2,"type":"ready"}"#) == .object(.otherVersion(2)), "an object of another version is told apart")
        report.expect(!isObject(line(#"{"v":1}"#)) && !isObject(line(#"{"type":"ready"}"#))
                          && !isObject(line(#"{"v":true,"type":"ready"}"#)),
                      "an object with no version or no type is dropped")
        report.expect(!isObject(line(#"{"v":1,"type":"controls","paused":1}"#)),
                      "a boolean field holding a number is not read as one")
        for sas in ["1234-567", "12345678", "abcd-efgh", "1234-56789", "1234 5678"] {
            let text = #"{"v":1,"type":"pair","state":"sas_pending","name":"p","sas":""# + sas + #"","deadline":"2026-11-01T12:00:00Z"}"#
            report.expect(!isObject(line(text)), "a SAS of another shape (\(sas)) is never read")
        }
        report.expect(line(#"{"v":1,"type":"version","host_version":"1","contract":"1.0"}"#) == .object(.unknown(type: "version")),
                      "a type the app does not use is ignored")

        // Values a reader treats specially (spec/CLI.md §12).
        let frozen = line(#"{"v":1,"type":"state","pair":"p","confirmed":true,"paused":false,"policy":"frozen","last_hub_contact":null,"clock":{"state":"unknown"},"rooms":[],"offers_waiting":0,"last_left_because":null}"#)
        if case .object(.state(let state)) = frozen {
            report.expect(state.policy == .invalid, "a policy state this release does not know is invalid")
        } else {
            report.expect(false, "a state with a new policy value reads", "\(frozen)")
        }
        let moon = line(#"{"v":1,"type":"stopped","reason":"drained","cause":"moon"}"#)
        report.expect(moon == .object(.stopped(.drained(cause: .other("moon")))), "a drain cause this release does not know is kept as other")
        report.expect(line(#"{"v":1,"type":"stopped","reason":"new_reason"}"#) == .object(.stopped(.other("new_reason"))),
                      "a stopped reason this release does not know is kept as other")
        report.expect(line(#"{"v":1,"type":"pair","state":"new_state","name":"p"}"#) == .object(.pair(.other("new_state"))),
                      "a pair state this release does not know is never shown")
        report.expect(line(#"{"v":1,"type":"error","code":"new_code","message":"the host's words"}"#)
                          == .object(.error(HostError(code: "new_code", message: "the host's words", kind: nil))),
                      "an error code this release does not know reads")

        // What Activity reads (spec/CLI.md §6): a request at each step, the log's lines, and the
        // operations' summaries. A request's phase is open; its decision and status are closed.
        report.expect(HostObject.knownTypes.isSuperset(of: ["request", "log_line", "op"]),
                      "Activity's types are among those the app reads", "\(HostObject.knownTypes.sorted())")
        let jobNine = "00000000-0000-4000-8000-000000000009"
        func request(_ rest: String) -> JSONLines.Item {
            line(#"{"v":1,"type":"request","job_id":"00000000-0000-4000-8000-000000000009","# + rest + "}")
        }
        report.expect(request(#""phase":"received","op":null,"root":null,"path":null"#)
                          == .object(.request(RequestEvent(jobId: jobNine, phase: .received(op: nil, root: nil, path: nil)))),
                      "a request the host could not read has no op, folder or path")
        report.expect(request(#""phase":"moon""#) == .object(.request(RequestEvent(jobId: jobNine, phase: .other("moon")))),
                      "a request phase this release does not know is kept as other")
        report.expect(request(#""phase":"finished","status":"abandoned""#)
                          == .object(.request(RequestEvent(jobId: jobNine, phase: .finished(.abandoned)))),
                      "an abandoned request reads")
        report.expect(request(#""phase":"finished","status":"failed","code":"killed_locally","exit_code":null,"duration_ms":5,"stdout_bytes":null,"stderr_bytes":null,"reply_bytes":300"#)
                          == .object(.request(RequestEvent(jobId: jobNine, phase: .finished(.failed("killed_locally"))))),
                      "a request that failed reads, with its code")
        for closed in [#""phase":"decided","decision":"later""#, #""phase":"decided","decision":"refused""#,
                       #""phase":"finished","status":"moon""#, #""phase":"finished","status":"refused""#] {
            report.expect(!isObject(request(closed)),
                          "a request with a decision or a status this release does not know, or with no code, is dropped", closed)
        }
        report.expect(!isObject(line(#"{"v":1,"type":"request","phase":"started"}"#)), "a request with no job is dropped")
        report.expect(line(#"{"v":1,"type":"log_line","text":"a line, ? as the host wrote it"}"#)
                          == .object(.logLine("a line, ? as the host wrote it")),
                      "a log line reads as the host wrote it")
        report.expect(!isObject(line(#"{"v":1,"type":"log_line"}"#)), "a log line with no text is dropped")
        report.expect(line(#"{"v":1,"type":"op","name":"host.nothing","state":"not_in_catalog","enabled":false}"#)
                          == .object(.op(name: "host.nothing", summary: nil)),
                      "an operation this release does not have reads, with no summary")

        // host_too_old's minimum is Lucille's, and the update notice shows it: it is read only in
        // the form the protocol gives it (spec/PROTOCOL.md §10), MAJOR.MINOR.PATCH, digits only,
        // each part at most 9 digits with no leading zero.
        func tooOld(_ least: String) -> JSONLines.Item {
            line(#"{"v":1,"type":"host_too_old","job_id":"j","min_host_version":""# + least + #"","host_version":"0.1.0.dev0"}"#)
        }
        for good in ["0.0.0", "1.2.3", "999.0.0", "10.20.30", "999999999.999999999.999999999"] {
            report.expect(tooOld(good) == .object(.hostTooOld(jobId: "j", minVersion: good, hostVersion: "0.1.0.dev0")),
                          "a minimum of \(good) reads")
        }
        let badMinimums = ["", "1", "1.2", "1.2.3.4", "01.2.3", "1.02.3", "1.2.03", "1000000000.0.0", "1.2.3-beta",
                           " 1.2.3", "1.2.3 ", "a.b.c", "1..3", "1.2.", ".1.2", "\u{0661}.\u{0662}.\u{0663}", "1.2.3\u{200B}"]
        for bad in badMinimums {
            report.expect(!isObject(tooOld(bad)), "a minimum of \(bad.debugDescription) is not read", "\(tooOld(bad))")
        }

        // Short verbs: how they ended comes first (spec/CLI.md §4.3).
        func result(_ status: Int32, _ stdout: String, signal: Bool = false) -> CLIResult {
            CLIResult(status: status, stdout: stdout, stderr: "", timedOut: false, cancelled: false,
                      terminationReason: signal ? .uncaughtSignal : .exit)
        }
        let held = #"{"v":1,"type":"error","code":"held","message":"m"}"# + "\n"
        report.expect(VerbOutcome(result(1, held)) == .refused(HostError(code: "held", message: "m", kind: nil)),
                      "exit 1 with an error object last is a refusal")
        report.expect(VerbOutcome(result(1, "")) == .signaled && VerbOutcome(result(2, ready + "\n")) == .signaled,
                      "exit 1 or 2 with no error object last was a signal")
        report.expect(VerbOutcome(result(1, held, signal: true)) == .signaled, "a signal is never read as an exit code")
        report.expect(VerbOutcome(result(0, #"{"v":1,"type":"controls","paused":true}"# + "\n")).paused == true,
                      "a controls object says whether the host is paused")
        report.expect(VerbOutcome(result(7, "")) == .otherExit(7), "another exit status is kept")
        report.expect(VerbOutcome(CLIResult(status: CLIResult.notStarted, stdout: "", stderr: "", timedOut: false)) == .notStarted
                          && VerbOutcome(CLIResult(status: 15, stdout: "", stderr: "", timedOut: true)) == .timedOut
                          && VerbOutcome(CLIResult(status: 15, stdout: "", stderr: "", timedOut: false, cancelled: true)) == .cancelled,
                      "a run that did not start, timed out or was cancelled says so")
        let signaled = result(24, "", signal: true)
        report.expect(signaled.signal == 24 && !signaled.succeeded && result(0, "").signal == nil,
                      "a result says which signal ended it")
    }

    static func isObject(_ item: JSONLines.Item) -> Bool {
        if case .object = item {
            return true
        }
        return false
    }

    static func checkFixtureObjects(_ fixtures: [String: Fixture], _ report: Report) {
        let deadline = sampleTime
        let pairing = fixtures["pair-code-stdin"]?.objects ?? []
        report.expect(pairing == [
            .pairCode(name: pairName), .policyReset,
            .pair(.accepted(name: pairName, clockSkewed: false)),
            .pair(.waiting(name: pairName)),
            .pair(.sasPending(name: pairName, sas: "1234-5678", deadline: deadline)),
        ], "pair --code-stdin reads as its code, the policy set aside, accepted, waiting, then the SAS", "\(pairing)")
        let leaked = fixtures["pair-code-stdin-leaked"]?.objects.last
        if case .error(let error)? = leaked {
            report.expect(error.code == "leaked", "a leaked code ends with its error")
        } else {
            report.expect(false, "pair-code-stdin-leaked ends with an error", "\(String(describing: leaked))")
        }
        report.expect(fixtures["pair-status-sas-pending"]?.objects
                          == [.pair(.sasPending(name: pairName, sas: "1234-5678", deadline: deadline))]
                          && fixtures["pair-status-waiting"]?.objects == [.pair(.waiting(name: pairName))]
                          && fixtures["pair-status-none"]?.objects == [.pair(.noPair)]
                          && fixtures["pair-confirm"]?.objects == [.pair(.confirmed(name: pairName))]
                          && fixtures["pair-reject"]?.objects == [.pair(.rejected(name: pairName, lucilleTold: true))],
                      "pair status, confirm and reject read")
        if case .status(let found)? = fixtures["status-paired"]?.objects.first {
            report.expect(found.pair == .confirmed(name: pairName) && found.run == .stopped && !found.paused
                              && found.policyOK && found.folders == ["docs"] && found.enabledOps == ["host.describe"]
                              && found.clock == .ok(skew: 1.5) && found.lastHubContact == deadline
                              && found.lastLeftBecause == nil,
                          "status, paired, reads its pair, run, policy and clock", "\(found)")
        } else {
            report.expect(false, "status-paired reads")
        }
        if case .status(let found)? = fixtures["status-unpaired"]?.objects.first {
            report.expect(found.pair == .noPair && found.run == .noRecord && found.clock == .unknown && found.lastHubContact == nil,
                          "status, unpaired, reads", "\(found)")
        } else {
            report.expect(false, "status-unpaired reads")
        }
        if case .status(let found)? = fixtures["status-unreadable"]?.objects.first {
            report.expect(found.pair == .unreadable(code: "insecure_keyring") && found.paused,
                          "status with an unreadable store says which, and the rest", "\(found)")
        } else {
            report.expect(false, "status-unreadable reads")
        }
        report.expect(fixtures["forget"]?.objects == [.forget(.forgotten(name: pairName, lucilleTold: true))]
                          && fixtures["pause"]?.objects == [.controls(paused: true)]
                          && fixtures["resume"]?.objects == [.controls(paused: false)]
                          && fixtures["kill"]?.objects == [.controls(paused: true)],
                      "forget, pause, resume and kill read")
        // Reset Settings: policy reset sets an invalid file aside; with nothing to set aside it
        // refuses not_invalid, which a shell resetting takes as done (spec/CLI.md §7).
        let reset = fixtures["policy-reset"]?.objects ?? []
        report.expect(reset == [.policyReset] && ResetOutcome(.done(reset)) == .reset,
                      "policy reset reads as the policy set aside", "\(reset)")
        let notInvalid = fixtures["policy-reset-not-invalid"]?.objects ?? []
        if case .error(let error)? = notInvalid.last, notInvalid.count == 1 {
            report.expect(error.code == "not_invalid" && ResetOutcome(.refused(error)) == .nothingToReset,
                          "a reset with nothing to set aside is not_invalid, which is done", "\(error)")
        } else {
            report.expect(false, "policy-reset-not-invalid is one error", "\(notInvalid)")
        }
        let locked = ResetOutcome(.refused(HostError(code: "policy_lock_failed", message: "m", kind: nil)))
        report.expect(ResetOutcome(.done([])) == .failed(.done([])) && ResetOutcome(.timedOut) == .failed(.timedOut)
                          && locked == .failed(.refused(HostError(code: "policy_lock_failed", message: "m", kind: nil))),
                      "an exit 0 with no policy_reset, a timeout and any other refusal are failures", "\(locked)")
        report.expect(locked.offersTryAgain && !ResetOutcome(.timedOut).offersTryAgain
                          && !ResetOutcome(.done([])).offersTryAgain && !ResetOutcome(.done(reset)).offersTryAgain,
                      "only a lock that could not be taken offers Try Again")
        // The host writes policy_reset only once the fresh policy is in place, so a run that wrote
        // it reset the settings, even when a Cancel or the time limit came after: HostCLI.run looks
        // for a Cancel before it looks for the exit.
        let resetOutput = (fixtures["policy-reset"]?.lines ?? []).map { String(decoding: $0, as: UTF8.self) + "\n" }.joined()
        let notInvalidOutput = (fixtures["policy-reset-not-invalid"]?.lines ?? []).map { String(decoding: $0, as: UTF8.self) + "\n" }.joined()
        let resetThenCancelled = CLIResult(status: 0, stdout: resetOutput, stderr: "", timedOut: false, cancelled: true)
        let resetThenLate = CLIResult(status: 15, stdout: resetOutput, stderr: "", timedOut: true)
        report.expect(ResetOutcome(result: CLIResult(status: 0, stdout: resetOutput, stderr: "", timedOut: false)) == .reset
                          && ResetOutcome(result: resetThenCancelled) == .reset && ResetOutcome(result: resetThenLate) == .reset,
                      "a run that wrote policy_reset is a reset, even when it was then cancelled or timed out", resetOutput)
        let cancelledFirst = CLIResult(status: 15, stdout: "", stderr: "", timedOut: false, cancelled: true)
        let lateFirst = CLIResult(status: 15, stdout: "", stderr: "", timedOut: true)
        let leftBehindReset = CLIResult(status: CLIResult.notFinished, stdout: "", stderr: "", timedOut: false, cancelled: true)
        report.expect(ResetOutcome(result: cancelledFirst) == .failed(.cancelled) && ResetOutcome(result: lateFirst) == .failed(.timedOut),
                      "a run cancelled or timed out before it wrote policy_reset reads as it ended")
        let notInvalidRun = CLIResult(status: 1, stdout: notInvalidOutput, stderr: "", timedOut: false)
        report.expect(ResetOutcome(result: leftBehindReset) == .failed(.notFinished) && ResetOutcome(result: notInvalidRun) == .nothingToReset,
                      "a run the app gave up on, and not_invalid, read as they ended", notInvalidOutput)
        let requests = fixtures["run-events-requests"]?.objects ?? []
        var hasRoom = false
        if requests.count > 1, case .state(let state) = requests[1] {
            hasRoom = state.confirmed && state.policy == .ok && state.rooms == [RunRoom(name: "room-1", state: .reading)]
        }
        report.expect(requests.first == .ready(pair: pairName, hostVersion: "0.1.0.dev0") && hasRoom
                          && requests.contains(.hostTooOld(jobId: "00000000-0000-4000-8000-000000000004",
                                                           minVersion: "999.0.0", hostVersion: "0.1.0.dev0"))
                          && requests.last == .stopped(.drained(cause: .stdinEnd))
                          && requests.filter({ (object: HostObject) -> Bool in
                              if case .request = object {
                                  return true
                              }
                              return false
                          }).count == 13,
                      "run's events read: ready, state, requests, host_too_old, stopped", "\(requests.count) objects")
        // The 13 request objects, field by field: four requests, each at each of its steps.
        func job(_ number: Int) -> String {
            "00000000-0000-4000-8000-00000000000\(number)"
        }
        let events = requests.compactMap { (object: HostObject) -> RequestEvent? in
            if case .request(let event) = object {
                return event
            }
            return nil
        }
        let expected: [RequestEvent] = [
            RequestEvent(jobId: job(1), phase: .received(op: "host.describe", root: nil, path: nil)),
            RequestEvent(jobId: job(1), phase: .decided(run: true, code: nil)),
            RequestEvent(jobId: job(1), phase: .finished(.done)),
            RequestEvent(jobId: job(2), phase: .received(op: "files.sha256", root: "docs", path: "a.txt")),
            RequestEvent(jobId: job(2), phase: .decided(run: true, code: nil)),
            RequestEvent(jobId: job(2), phase: .started),
            RequestEvent(jobId: job(2), phase: .finished(.done)),
            RequestEvent(jobId: job(3), phase: .received(op: "files.sha256", root: "docs", path: nil)),
            RequestEvent(jobId: job(3), phase: .decided(run: false, code: "path_outside_root")),
            RequestEvent(jobId: job(3), phase: .finished(.refused("path_outside_root"))),
            RequestEvent(jobId: job(4), phase: .received(op: "host.describe", root: nil, path: nil)),
            RequestEvent(jobId: job(4), phase: .decided(run: false, code: "host_too_old")),
            RequestEvent(jobId: job(4), phase: .finished(.refused("host_too_old"))),
        ]
        report.expect(events == expected, "each request object reads with its job, its phase and that phase's fields", "\(events)")
    }

    // MARK: - checkRequestLog

    /// Activity (decisions/0002 §15): `run`'s `request` events kept in memory, one entry per
    /// request, updated in place and closed as stopped when `run` ends; what Lucille asked for in
    /// words, its path quoted or not shown at all; and no sentence that says a reply was received.
    static func checkRequestLog(_ fixtures: [String: Fixture], _ report: Report) {
        let start = Date(timeIntervalSince1970: 1_800_000_000)
        func at(_ seconds: Int) -> Date {
            start.addingTimeInterval(TimeInterval(seconds))
        }
        func jobId(_ number: Int) -> String {
            let digits = String(number)
            return "00000000-0000-4000-8000-" + String(repeating: "0", count: max(0, 12 - digits.count)) + digits
        }
        func event(_ number: Int, _ phase: RequestPhase) -> RequestEvent {
            RequestEvent(jobId: jobId(number), phase: phase)
        }
        func outcome(_ log: RequestLog) -> String? {
            log.entries.first.flatMap { RequestText.outcome(for: $0) }
        }
        func lineOf(_ phase: RequestPhase, _ summaries: [String: String]) -> String {
            var one = RequestLog()
            one.note(event(30, phase), at: at(1))
            return one.entries.first.map { RequestText.line(for: $0, summaries: summaries) } ?? ""
        }
        let summaries = ["host.describe": "Describe this computer's host", "files.sha256": "Check a file's fingerprint"]
        let fingerprint = RequestPhase.received(op: "files.sha256", root: "docs", path: "a.txt")
        // Every sentence Activity said here, for the last check.
        var said: [String] = []

        // The summaries and the log's lines, from the host's own fixtures.
        if !fixtures.isEmpty {
            let listed = VerbOutcome.done(fixtures["ops-list"]?.objects ?? []).summaries
            report.expect(listed == ["host.describe": "Describe this computer's host", "host.sleep": "Wait up to 4 minutes, for testing",
                                     "files.sha256": "Check a file's fingerprint"],
                          "ops list --json gives each operation's summary, in the core's words", "\(listed)")
            let every = VerbOutcome.done(fixtures["ops-list-every-state"]?.objects ?? []).summaries
            report.expect(every.count == 3 && every["host.nothing"] == nil,
                          "a policy entry this release does not have has no summary", "\(every)")
            report.expect(VerbOutcome.done(fixtures["log"]?.objects ?? []).logLines
                              == [#"{"kind":"test","n":1}"#, #"{"kind":"test","n":2}"#],
                          "log --json gives its lines as the host wrote them")
            report.expect(fixtures["log-empty"] != nil && VerbOutcome.done(fixtures["log-empty"]?.objects ?? []).logLines.isEmpty,
                          "an empty log gives no line")
        }
        report.expect(VerbOutcome.refused(HostError(code: "bad_limit", message: "m", kind: nil)).logLines.isEmpty,
                      "a log verb that was refused gives no line")
        let wide = String(repeating: "w", count: 5000)
        report.expect(RequestText.logLine(wide).count == 2000 && RequestText.logLine(wide).hasSuffix("\u{2026}")
                          && RequestText.logLine(#"{"kind":"test","n":1}"#) == #"{"kind":"test","n":1}"#,
                      "a log line is shown as written, cut to 2000 characters")

        // run's events, as the fixture has them: four requests.
        if let objects = fixtures["run-events-requests"]?.objects {
            var log = RequestLog()
            var second = 0
            var runningAfter: [String] = []
            for object in objects {
                guard case .request(let found) = object else {
                    continue
                }
                second += 1
                log.note(found, at: at(second))
                if log.operationRunning {
                    runningAfter.append("\(found.jobId) \(found.phase)")
                }
            }
            report.expect(log.entries.map(\.jobId) == [jobId(1), jobId(2), jobId(3), jobId(4)],
                          "one entry per request, oldest first", "\(log.entries.map(\.jobId))")
            report.expect(log.entries.map(\.firstSeen) == [at(1), at(4), at(8), at(11)],
                          "each entry carries the app's clock of its first event", "\(log.entries.map(\.firstSeen))")
            report.expect(runningAfter == ["\(jobId(2)) started"],
                          "an operation runs only from its started to its finished", "\(runningAfter)")
            let lines = log.entries.map { RequestText.line(for: $0, summaries: summaries) }
            let ends = log.entries.map { RequestText.outcome(for: $0) }
            report.expect(lines == [
                "Lucille asked to describe this computer's host.",
                "Lucille asked to check a file's fingerprint: \u{201C}a.txt\u{201D} in docs.",
                "Lucille asked to check a file's fingerprint: \(RequestText.unshownPath) in docs.",
                "Lucille asked to describe this computer's host.",
            ], "each request in words, from the core's summary, with Lucille's path quoted", "\(lines)")
            let wanted: [String?] = [
                RequestText.finished, RequestText.finished,
                RequestText.refused["path_outside_root"], RequestText.refused["host_too_old"],
            ]
            let newer = RequestText.refused["host_too_old"] ?? ""
            report.expect(ends == wanted && newer.contains(HostText.appName),
                          "each says how it ended: finished, or turned away for its path, or for a newer app", "\(ends)")
            log.runEnded(at: at(20))
            report.expect(log.entries.map { RequestText.outcome(for: $0) } == ends && !log.operationRunning,
                          "run ending changes no request that finished")
            said += lines + ends.compactMap { $0 }
        }

        // A request with no finished when run ends is closed as stopped.
        var open = RequestLog()
        open.note(event(10, .received(op: "host.sleep", root: nil, path: nil)), at: at(1))
        report.expect(!open.operationRunning && outcome(open) == nil, "a request just received says nothing more yet")
        open.note(event(10, .decided(run: true, code: nil)), at: at(2))
        open.note(event(10, .started), at: at(3))
        report.expect(open.operationRunning && outcome(open) == RequestText.running && RequestText.running == "Running\u{2026}",
                      "a request that started runs")
        open.note(event(10, .other("moon")), at: at(4))
        open.note(event(11, .other("moon")), at: at(4))
        report.expect(open.operationRunning && open.entries.count == 1 && outcome(open) == RequestText.running,
                      "a phase this release does not know is ignored, and starts no entry")
        open.runEnded(at: at(5))
        report.expect(!open.operationRunning && outcome(open) == RequestText.stoppedWithRun
                          && RequestText.stoppedWithRun == "Stopped when \(HostText.appName)'s helper program stopped.",
                      "when run ends, a request with no finished is closed as stopped, and nothing runs")
        var waiting = RequestLog()
        waiting.note(event(13, .received(op: "host.describe", root: nil, path: nil)), at: at(1))
        waiting.note(event(14, .received(op: "host.describe", root: nil, path: nil)), at: at(1))
        waiting.note(event(14, .decided(run: true, code: nil)), at: at(2))
        waiting.runEnded(at: at(3))
        let waitingEnds = waiting.entries.map { RequestText.outcome(for: $0) ?? "nil" }
        report.expect(waitingEnds == [RequestText.stoppedWithRun, RequestText.stoppedWithRun] && !waiting.operationRunning,
                      "so is one not decided yet, and one decided to run and not started", "\(waitingEnds)")
        // Written again from received after a restart: the same entry, updated in place.
        open.note(event(10, .received(op: "host.sleep", root: nil, path: nil)), at: at(6))
        report.expect(open.entries.count == 1 && open.entries.first?.firstSeen == at(1) && outcome(open) == nil,
                      "a request written again from received is the same entry, open again, first seen as before")
        var moved = RequestLog()
        moved.note(event(12, fingerprint), at: at(1))
        moved.note(event(12, .decided(run: true, code: nil)), at: at(2))
        moved.note(event(12, .received(op: "files.sha256", root: "docs", path: "b.txt")), at: at(3))
        let now = moved.entries.first.map { RequestText.line(for: $0, summaries: summaries) } ?? ""
        report.expect(moved.entries.count == 1 && now.contains("\u{201C}b.txt\u{201D}") && !now.contains("a.txt"),
                      "and says what it asks for now", now)
        // A request that ended is never written again: a received for its job id, which the host
        // forgets 15 minutes after the request's deadline, is a new request, and the one that
        // ended keeps what it said.
        var reused = RequestLog()
        reused.note(event(15, fingerprint), at: at(1))
        reused.note(event(15, .decided(run: true, code: nil)), at: at(2))
        reused.note(event(15, .finished(.done)), at: at(3))
        reused.note(event(15, .received(op: "host.describe", root: nil, path: nil)), at: at(1300))
        reused.note(event(15, .decided(run: false, code: "not_enabled")), at: at(1301))
        let both = reused.entries.map { RequestText.line(for: $0, summaries: summaries) + " " + (RequestText.outcome(for: $0) ?? "") }
        let reusedSeen = reused.entries.map(\.firstSeen)
        report.expect(both == [
            "Lucille asked to check a file's fingerprint: \u{201C}a.txt\u{201D} in docs. Finished on this Mac.",
            "Lucille asked to describe this computer's host. Turned away: that ability is off on this Mac.",
        ] && reusedSeen == [at(1), at(1300)],
                      "a job id used again after its request ended starts an entry of its own, and the first keeps what it said", "\(both)")

        // How each request ends, in words.
        let endings: [(RequestPhase, String?)] = [
            (.finished(.done), "Finished on this Mac."),
            (.finished(.abandoned), "Dropped: the connection changed."),
            (.decided(run: false, code: "not_enabled"), "Turned away: that ability is off on this Mac."),
            (.finished(.refused("not_enabled")), "Turned away: that ability is off on this Mac."),
            (.finished(.failed("killed_locally")), RequestText.failed["killed_locally"]),
            (.finished(.failed("timeout")), RequestText.failed["timeout"]),
            (.finished(.failed("interrupted")), RequestText.failed["interrupted"]),
        ]
        for (number, ending) in endings.enumerated() {
            var one = RequestLog()
            one.note(event(20 + number, fingerprint), at: at(1))
            one.note(event(20 + number, ending.0), at: at(2))
            let got = outcome(one)
            report.expect(ending.1 != nil && got == ending.1 && !one.operationRunning,
                          "\(ending.0) reads as \(ending.1 ?? "nothing")", got ?? "nil")
            said += got.map { [$0] } ?? []
        }
        for ending in [RequestPhase.finished(.refused("brand_new")), .finished(.failed("brand_new"))] {
            var one = RequestLog()
            one.note(event(40, fingerprint), at: at(1))
            one.note(event(40, ending), at: at(2))
            let got = outcome(one) ?? ""
            report.expect(!got.isEmpty && !got.contains("brand_new"), "a code this release does not know still has words, not the code", got)
            said.append(got)
        }

        // What Lucille asked for, in words: quoted, cut, or not shown at all.
        report.expect(lineOf(.received(op: "new.op", root: nil, path: nil), summaries) == "Lucille asked for \u{201C}new.op\u{201D}.",
                      "an operation the list lacks is named, in quotes")
        report.expect(lineOf(.received(op: nil, root: nil, path: nil), summaries) == RequestText.unreadableRequest
                          && RequestText.unreadableRequest == "Lucille sent a request this Mac couldn't read.",
                      "a request the host could not read says so")
        report.expect(lineOf(fingerprint, [:]) == "Lucille asked for \u{201C}files.sha256\u{201D}: \u{201C}a.txt\u{201D} in docs.",
                      "with no summaries, the operation is named, and its path and folder still said")
        report.expect(lineOf(.received(op: "files.sha256", root: "Not An Alias", path: "a.txt"), summaries)
                          == "Lucille asked to check a file's fingerprint: \u{201C}a.txt\u{201D}.",
                      "a folder name that is not an alias is left out")
        report.expect(lineOf(.received(op: "files.sha256", root: "docs", path: "a\u{202E}txt.exe"), summaries)
                          == "Lucille asked to check a file's fingerprint: \(RequestText.unshownPath) in docs."
                          && RequestText.unshownPath == "a path that can't be shown",
                      "a path that cannot be shown safely is not shown")
        report.expect(RequestText.quoted("a.txt") == "\u{201C}a.txt\u{201D}", "Lucille's path is shown in quotes")
        report.expect(RequestText.quoted("it's a \"b\".txt") == "\u{201C}it's a \"b\".txt\u{201D}",
                      "a straight apostrophe or quote, which cannot end the app's quotes, is shown")
        let hidden: [(String, String)] = [
            ("a right-to-left override", "a\u{202E}txt.exe"), ("a zero-width space", "a\u{200B}b"),
            ("a byte order mark", "\u{FEFF}a"), ("a line separator", "a\u{2028}b"),
            ("a paragraph separator", "a\u{2029}b"), ("a bell", "a\u{7}b"), ("a newline", "a\nb"),
            ("a delete", "a\u{7F}b"), ("a closing curly quote", "x\u{201D} in docs. Finished on this Mac./../a.txt"),
            ("an opening curly quote", "\u{201C}a"), ("a curly apostrophe", "it\u{2019}s.txt"), ("a guillemet", "a\u{00BB}b"),
        ]
        for (what, text) in hidden {
            report.expect(RequestText.quoted(text) == nil, "a path with \(what) is not shown")
        }
        // A quotation mark of another category, which can still look like the end of the app's
        // quotes, is refused by the list written out in RequestText (the review of #14): the
        // path behind each of these would otherwise read as the app's own sentence.
        let lookalikes: [(String, UInt32)] = [("U+201E", 0x201E), ("U+FF02", 0xFF02), ("U+2033", 0x2033), ("U+2036", 0x2036)]
        for (name, value) in lookalikes {
            let mark = Unicode.Scalar(value).map { String(Character($0)) } ?? ""
            report.expect(!mark.isEmpty && RequestText.quoted("a.txt" + mark + " in documents. Finished on this Mac.") == nil,
                          "a path that imitates the app's own words behind \(name) is not shown")
            report.expect(RequestText.quoteLookalikes.contains(value), "\(name) is one of the quote look-alikes")
        }
        report.expect(RequestText.quoted("a.txt\" in documents. Finished on this Mac.") == "\u{201C}a.txt\" in documents. Finished on this Mac.\u{201D}"
                          && RequestText.quoted("it's.txt") == "\u{201C}it's.txt\u{201D}",
                      "a straight quote or apostrophe, which cannot end the app's quotes, is still shown")
        let shownAnyway = RequestText.quoteLookalikes.sorted().filter { value in
            guard let scalar = Unicode.Scalar(value) else {
                return true
            }
            return RequestText.quoted("a" + String(Character(scalar)) + "b") != nil
        }
        report.expect(RequestText.quoteLookalikes.count > 40 && shownAnyway.isEmpty
                          && !RequestText.quoteLookalikes.contains(0x22) && !RequestText.quoteLookalikes.contains(0x27),
                      "every quote look-alike is refused, and the straight quote and apostrophe are not among them",
                      "\(shownAnyway)")
        let long = "start-" + String(repeating: "x", count: 1010) + "-end.txt"
        let cut = RequestText.quoted(long) ?? ""
        report.expect(long.count == 1024 && cut.count == 62 && cut.hasPrefix("\u{201C}start-") && cut.hasSuffix("-end.txt\u{201D}")
                          && cut.contains("\u{2026}"),
                      "a path of 1024 characters is cut to 60, in the middle", cut)
        let sixty = String(repeating: "p", count: 60)
        report.expect(RequestText.quoted(sixty) == "\u{201C}" + sixty + "\u{201D}", "one of 60 is shown whole")

        // At most 500 requests are kept.
        var full = RequestLog()
        for number in 1...501 {
            full.note(event(1000 + number, .received(op: "host.describe", root: nil, path: nil)), at: at(number))
        }
        report.expect(RequestLog.capacity == 500 && full.entries.count == 500
                          && full.entries.first?.jobId == jobId(1002) && full.entries.last?.jobId == jobId(1501),
                      "at most 500 requests are kept, the oldest dropped", "\(full.entries.count)")
        var busy = RequestLog()
        busy.note(event(2000, .received(op: "host.sleep", root: nil, path: nil)), at: at(0))
        busy.note(event(2000, .decided(run: true, code: nil)), at: at(0))
        busy.note(event(2000, .started), at: at(0))
        for number in 1...500 {
            busy.note(event(2000 + number, .received(op: "host.describe", root: nil, path: nil)), at: at(number))
        }
        report.expect(busy.entries.count == 500 && busy.operationRunning && busy.entries.first?.jobId == jobId(2000)
                          && busy.entries.dropFirst().first?.jobId == jobId(2002),
                      "a request whose process runs is kept while newer ones go: Activity still shows that it runs",
                      "\(busy.entries.prefix(2).map(\.jobId))")

        said += [RequestText.running, RequestText.finished, RequestText.abandoned, RequestText.stoppedWithRun,
                 RequestText.unreadableRequest, RequestText.unshownPath, ConnectionText.activityHeader]
        said += Array(RequestText.refused.values) + Array(RequestText.failed.values)
        report.expect(said.count > 20 && said.allSatisfy { !$0.lowercased().contains("received") },
                      "no sentence says anything was received: finished means finished on this Mac")
    }

    // MARK: - checkPairing

    static func checkPairing(_ fixtures: [String: Fixture], _ report: Report) {
        let deadline = sampleTime
        let code = "swbp1_" + String(repeating: "Q", count: 160)
        let pairObjects: [HostObject] = fixtures["pair-code-stdin"]?.objects ?? [
            .pairCode(name: pairName), .policyReset,
            .pair(.accepted(name: pairName, clockSkewed: false)),
            .pair(.waiting(name: pairName)),
            .pair(.sasPending(name: pairName, sas: "1234-5678", deadline: deadline)),
        ]
        let pending = status(.sasPending(name: pairName, deadline: deadline))

        // The whole gate, as the fixture runs it.
        var flow = PairingFlow()
        func send(_ input: PairingFlow.Input) -> [PairingFlow.Effect] {
            flow.handle(input, epoch: flow.epoch)
        }
        _ = send(.startConnecting)
        report.expect(flow.screen == .enterCode, "Connect to Lucille… opens the code window")
        report.expect(send(.submit("hello")).isEmpty && flow.screen == .enterCode && !flow.pairChildAlive,
                      "text that is not a code starts nothing")
        let started = send(.submit("  " + code + "\n"))
        report.expect(started == [.clearCodeField, .startPair(code: code)] && flow.pairChildAlive,
                      "a code starts pair --code-stdin with the code, and the field is cleared", "\(started)")
        var shown: [PairingFlow.Effect] = []
        for object in pairObjects {
            shown += send(.childObject(object))
        }
        let expected = PairingFlow.Comparison(name: pairName, sas: "1234-5678", deadline: deadline, canMatch: true, drawnAt: nil)
        report.expect(flow.screen == .compare(expected) && shown == [.cancelPoll, .deadlineAt(deadline.addingTimeInterval(Pairing.deadlineGrace))],
                      "sas_pending shows the number, and sets a timer a little past its deadline", "\(flow.screen) \(shown)")
        report.expect(send(.childExited(.exited(0))).isEmpty && !flow.pairChildAlive, "pair exits 0 once the SAS is shown")
        report.expect(send(.theyMatch(at: 100)).isEmpty && flow.screen == .compare(expected),
                      "They Match before the number is drawn does nothing")
        report.expect(send(.sasDrawn("0000-0000", at: 100)).isEmpty && flow.screen == .compare(expected),
                      "another number drawn does not count")
        _ = send(.sasDrawn("1234-5678", at: 100))
        report.expect(send(.theyMatch(at: 100.1)).isEmpty, "a click from before the number was drawn does not count")
        let matched = send(.theyMatch(at: 101))
        report.expect(matched == [.cancelDeadline, .run(.status(.beforeConfirm))] && flow.screen == .busy(.checking),
                      "They Match first reads status, which never carries the SAS", "\(matched)")
        let bound = send(.verbResult(.status(.beforeConfirm), .done([.status(pending)])))
        report.expect(bound == [.run(.pairConfirm)] && flow.screen == .busy(.confirming),
                      "the same pair, with the same deadline, still pending: pair confirm", "\(bound)")
        let confirmed = send(.verbResult(.pairConfirm, .done([.pair(.confirmed(name: pairName))])))
        report.expect(confirmed == [.say(.confirmed), .startRunCheck] && flow.screen == .closed,
                      "confirmed: the note, and a status read that may start run", "\(confirmed)")

        // A second sas_pending must be drawn again; a status that disagrees redraws.
        func comparing() -> PairingFlow {
            var fresh = PairingFlow()
            _ = fresh.handle(.startConnecting, epoch: fresh.epoch)
            _ = fresh.handle(.submit(code), epoch: fresh.epoch)
            _ = fresh.handle(.childObject(.pair(.sasPending(name: pairName, sas: "1234-5678", deadline: deadline))), epoch: fresh.epoch)
            _ = fresh.handle(.childExited(.exited(0)), epoch: fresh.epoch)
            _ = fresh.handle(.sasDrawn("1234-5678", at: 10), epoch: fresh.epoch)
            return fresh
        }
        flow = comparing()
        _ = send(.childObject(.pair(.sasPending(name: pairName, sas: "8765-4321", deadline: deadline))))
        if case .compare(let now) = flow.screen {
            report.expect(now.sas == "8765-4321" && !now.drawn, "a new sas_pending must be drawn again", "\(now)")
        } else {
            report.expect(false, "a new sas_pending shows", "\(flow.screen)")
        }
        report.expect(send(.theyMatch(at: 50)).isEmpty, "and They Match waits for it")
        for other in [status(.sasPending(name: pairName, deadline: deadline.addingTimeInterval(60))),
                      status(.sasPending(name: "pair-other", deadline: deadline)),
                      status(.sasReady(name: pairName))] {
            flow = comparing()
            _ = send(.theyMatch(at: 20))
            let redraw = send(.verbResult(.status(.beforeConfirm), .done([.status(other)])))
            report.expect(redraw == [.run(.pairStatus)] && flow.screen == .waiting(clockSkewed: false),
                          "a status that names another pair, deadline or an unshown SAS confirms nothing, and redraws", "\(other.pair) \(redraw)")
        }
        flow = comparing()
        _ = send(.theyMatch(at: 20))
        report.expect(send(.verbResult(.status(.beforeConfirm), .timedOut)) == [.run(.pairStatus)],
                      "a status that did not answer confirms nothing")
        flow = comparing()
        _ = send(.theyMatch(at: 20))
        report.expect(send(.verbResult(.status(.beforeConfirm), .done([.status(status(.confirmed(name: pairName)))])))
                          == [.startRunCheck] && flow.screen == .closed,
                      "the pair shown, confirmed meanwhile: the run check, and no confirm")
        flow = comparing()
        _ = send(.theyMatch(at: 20))
        report.expect(send(.verbResult(.status(.beforeConfirm), .done([.status(status(.confirmed(name: "pair-other")))])))
                          == [.say(.confirmedAnother), .refreshStatus] && flow.screen == .closed,
                      "another pair confirmed meanwhile: said, and nothing started")
        flow = comparing()
        _ = send(.theyMatch(at: 20))
        _ = send(.verbResult(.status(.beforeConfirm), .done([.status(pending)])))
        report.expect(send(.verbResult(.pairConfirm, .done([.pair(.confirmed(name: "pair-other"))])))
                          == [.say(.confirmedAnother), .refreshStatus],
                      "a confirm that names another pair says so, and starts nothing")
        flow = comparing()
        _ = send(.theyMatch(at: 20))
        _ = send(.verbResult(.status(.beforeConfirm), .done([.status(pending)])))
        report.expect(send(.verbResult(.pairConfirm, .refused(HostError(code: "sas_ready", message: "m", kind: nil))))
                          == [.run(.pairStatus)],
                      "sas_ready from pair confirm: ask pair status, and draw it")
        let answers: [(VerbOutcome, [PairingFlow.Effect])] = [
            (.done([.status(status(.confirmed(name: pairName)))]), [.say(.confirmed), .startRunCheck]),
            (.done([.status(pending)]), [.say(.notConfirmed), .refreshStatus]),
            (.timedOut, [.say(.confirmUnknown), .refreshStatus]),
        ]
        for (answer, says) in answers {
            flow = comparing()
            _ = send(.theyMatch(at: 20))
            _ = send(.verbResult(.status(.beforeConfirm), .done([.status(pending)])))
            let unanswered = send(.verbResult(.pairConfirm, .cancelled))
            let after = send(.verbResult(.status(.afterConfirm), answer))
            report.expect(unanswered == [.run(.status(.afterConfirm))] && after == says,
                          "a confirm with no answer asks status, and says plainly what it found", "\(after)")
        }

        // Each status read is answered once: one asked before a confirm, arriving after the confirm
        // ended with no answer, confirms nothing again.
        flow = comparing()
        _ = send(.theyMatch(at: 20))
        _ = send(.verbResult(.status(.beforeConfirm), .done([.status(pending)])))
        _ = send(.verbResult(.pairConfirm, .cancelled))
        report.expect(send(.verbResult(.status(.beforeConfirm), .done([.status(pending)]))).isEmpty && flow.screen == .busy(.checking),
                      "a second answer to the read before a confirm runs no second confirm")
        report.expect(send(.verbResult(.status(.afterConfirm), .done([.status(pending)]))) == [.say(.notConfirmed), .refreshStatus],
                      "and the read after it is still answered")

        // They Differ: reject, a late answer from before is dropped, and They Match stays off.
        flow = comparing()
        let before = flow.epoch
        let differ = send(.theyDiffer)
        report.expect(differ == [.cancelRunRetries, .cancelPoll, .cancelDeadline, .run(.pairReject)] && flow.epoch == before + 1,
                      "They Differ runs pair reject in a new epoch", "\(differ)")
        report.expect(flow.handle(.verbResult(.pairStatus, .done([.pair(.sasPending(name: pairName, sas: "1234-5678", deadline: deadline))])),
                                  epoch: before).isEmpty && flow.screen == .busy(.rejecting),
                      "an answer from before They Differ is dropped")
        report.expect(send(.verbResult(.pairReject, .done([.pair(.rejected(name: pairName, lucilleTold: true))])))
                          == [.say(.rejected(lucilleTold: true, afterDiffer: true)), .refreshStatus],
                      "rejected: Nothing was connected, and whether Lucille was told")
        flow = comparing()
        _ = send(.theyDiffer)
        let failed = send(.verbResult(.pairReject, .refused(HostError(code: "room_not_left", message: "m", kind: nil))))
        report.expect(failed == [.say(.rejectFailed(HostError(code: "room_not_left", message: "m", kind: nil))), .refreshStatus],
                      "a reject that failed says so", "\(failed)")
        _ = send(.resume(.sasPending(name: pairName, deadline: deadline)))
        _ = send(.verbResult(.pairStatus, .done([.pair(.sasPending(name: pairName, sas: "1234-5678", deadline: deadline))])))
        _ = send(.sasDrawn("1234-5678", at: 30))
        if case .compare(let latched) = flow.screen {
            report.expect(!latched.canMatch && send(.theyMatch(at: 40)).isEmpty,
                          "after They Differ, They Match is never on again for that pair", "\(latched)")
        } else {
            report.expect(false, "the differed pair shows again, to stop it", "\(flow.screen)")
        }

        // A relaunch resumes from status (spec/CLI.md §7).
        let relaunches: [(StatusPair, [PairingFlow.Effect], PairingFlow.Screen)] = [
            (.noPair, [], .closed),
            (.waiting(name: pairName), [.cancelRunRetries, .cancelPoll, .cancelDeadline, .run(.pairStatus)], .waiting(clockSkewed: false)),
            (.sasReady(name: pairName), [.cancelRunRetries, .cancelPoll, .cancelDeadline, .run(.pairStatus)], .waiting(clockSkewed: false)),
            (.sasPending(name: pairName, deadline: deadline), [.cancelRunRetries, .cancelPoll, .cancelDeadline, .run(.pairStatus)], .waiting(clockSkewed: false)),
            (.expired(name: pairName, deadline: deadline), [.say(.refused(code: "expired", message: nil, afterSAS: true)), .refreshStatus], .closed),
            (.refused(name: pairName), [.say(.refused(code: "no_gate", message: nil, afterSAS: false))], .closed),
            (.confirmed(name: pairName), [], .closed),
            (.severalPairs, [], .closed),
            (.unreadable(code: "store_error"), [], .closed),
        ]
        for (pair, effects, screen) in relaunches {
            flow = PairingFlow()
            let got = send(.resume(pair))
            report.expect(got == effects && flow.screen == screen, "a relaunch with \(pair) resumes as it should", "\(got) \(flow.screen)")
        }
        flow = PairingFlow()
        _ = send(.resume(.waiting(name: pairName)))
        _ = send(.resume(.waiting(name: pairName)))
        let staleAnswer = flow.handle(
            .verbResult(.pairStatus, .done([.pair(.sasPending(name: pairName, sas: "1234-5678", deadline: deadline))])),
            epoch: flow.epoch - 1
        )
        report.expect(staleAnswer.isEmpty && flow.screen == .waiting(clockSkewed: false),
                      "a pair status answer from an earlier attempt changes nothing, even on the same page",
                      "\(staleAnswer) \(flow.screen)")
        flow = PairingFlow()
        _ = send(.resume(.waiting(name: pairName)))
        let polled = send(.verbResult(.pairStatus, .done([.pair(.waiting(name: pairName))])))
        report.expect(polled == [.pollAfter(5)] && send(.pollDue) == [.run(.pairStatus)],
                      "while waiting, pair status asks again 5 s after each answer")
        report.expect(send(.windowClosed) == [.cancelPoll, .cancelDeadline] && flow.screen == .closed,
                      "closing the window stops asking")
        report.expect(send(.pollDue).isEmpty && send(.verbResult(.pairStatus, .done([.pair(.waiting(name: pairName))]))).isEmpty,
                      "and nothing asks pair status while it is closed")
        let late = send(.verbResult(.pairStatus, .done([.pair(.sasPending(name: pairName, sas: "1234-5678", deadline: deadline))])))
        if case .compare = flow.screen {
            report.expect(late.contains(.deadlineAt(deadline.addingTimeInterval(Pairing.deadlineGrace))),
                          "a SAS that arrives after the window closed brings it back, to show it")
        } else {
            report.expect(false, "a SAS that arrives after the window closed is shown", "\(flow.screen)")
        }

        // The pairing child: closing the window or its time limit interrupts it, which keeps the pair.
        flow = PairingFlow()
        _ = send(.startConnecting)
        _ = send(.submit(code))
        report.expect(send(.windowClosed) == [.cancelPoll, .cancelDeadline, .interruptPair], "closing the window interrupts pair")
        _ = send(.childObject(.error(HostError(code: "interrupted", message: "m", kind: nil))))
        report.expect(send(.childExited(.exited(1))) == [.refreshStatus] && flow.screen == .closed,
                      "interrupted: the pair is kept, and the menu offers Finish Connecting…")
        flow = PairingFlow()
        _ = send(.startConnecting)
        _ = send(.submit(code))
        report.expect(send(.childTimeLimit) == [.interruptPair], "the child's time limit interrupts it")
        _ = send(.childObject(.error(HostError(code: "interrupted", message: "m", kind: nil))))
        report.expect(send(.childExited(.exited(1))) == [.run(.pairStatus)], "and pair status goes on while the window is open")
        flow = PairingFlow()
        _ = send(.startConnecting)
        _ = send(.submit(code))
        let stop = send(.stopConnecting)
        report.expect(stop == [.cancelRunRetries, .cancelPoll, .cancelDeadline, .interruptPair],
                      "Stop Connecting interrupts a waiting pair first", "\(stop)")
        report.expect(flow.handle(.childExited(.exited(1)), epoch: flow.epoch - 1) == [.run(.pairReject)],
                      "then rejects once it has ended")
        report.expect(send(.verbResult(.pairReject, .done([.pair(.rejected(name: pairName, lucilleTold: false))])))
                          == [.say(.rejected(lucilleTold: false, afterDiffer: false)), .refreshStatus],
                      "and says Lucille was not told")
        flow = PairingFlow()
        _ = send(.startConnecting)
        _ = send(.submit(code))
        report.expect(send(.childNotStarted) == [.say(.notStarted), .refreshStatus], "a child that cannot start says so")
        flow = PairingFlow()
        _ = send(.startConnecting)
        _ = send(.submit(code))
        report.expect(send(.childExited(.signaled(15))) == [.run(.status(.afterChild))],
                      "a child that ends with no answer: status says where the pair is")
        report.expect(send(.verbResult(.status(.afterChild), .done([.status(status(.waiting(name: pairName)))]))) == [.run(.pairStatus)],
                      "and the window goes on from there")
        for (confirmedName, says) in [(pairName, [PairingFlow.Effect.startRunCheck]),
                                      ("pair-other", [.say(.confirmedAnother), .refreshStatus])] {
            flow = PairingFlow()
            _ = send(.startConnecting)
            _ = send(.submit(code))
            _ = send(.childObject(.pairCode(name: pairName)))
            _ = send(.childExited(.signaled(15)))
            let found = send(.verbResult(.status(.afterChild), .done([.status(status(.confirmed(name: confirmedName)))])))
            report.expect(found == says && flow.screen == .closed,
                          "a child that ended with \(confirmedName) confirmed: \(says)", "\(found)")
        }

        // Every refusal pair --code-stdin can give, and which ones bring the code window back.
        var codes = Array(ConnectionText.refusals.keys) + Array(ConnectionText.appBugCodes)
        codes.append("brand_new")
        for refusal in codes.sorted() where refusal != "interrupted" {
            flow = PairingFlow()
            _ = send(.startConnecting)
            _ = send(.submit(code))
            _ = send(.childObject(.error(HostError(code: refusal, message: "the host's words", kind: nil))))
            let said = send(.childExited(.exited(1)))
            let again = Pairing.newCodeCodes.contains(refusal)
            report.expect(said.contains(.say(.refused(code: refusal, message: "the host's words", afterSAS: false)))
                              && flow.screen == (again ? PairingFlow.Screen.enterCode : .closed),
                          "\(refusal) is said, and \(again ? "the code window stays" : "the window closes")", "\(said)")
        }
        let leakedObjects = fixtures["pair-code-stdin-leaked"]?.objects ?? []
        if !leakedObjects.isEmpty {
            flow = PairingFlow()
            _ = send(.startConnecting)
            _ = send(.submit(code))
            for object in leakedObjects {
                _ = send(.childObject(object))
            }
            let said = send(.childExited(.exited(1)))
            var leakedSaid = false
            for effect in said {
                if case .say(.refused(let found, _, false)) = effect, found == "leaked" {
                    leakedSaid = true
                }
            }
            report.expect(leakedSaid, "the leaked fixture is said as leaked", "\(said)")
        }

        // The deadline: pair status a little past it says expired, after a SAS was shown.
        flow = comparing()
        report.expect(send(.deadlinePassed(at: deadline.addingTimeInterval(3))) == [.run(.pairStatus)], "the deadline asks pair status")
        let expiredSaid = send(.verbResult(.pairStatus, .refused(HostError(code: "expired", message: "m", kind: nil))))
        report.expect(expiredSaid.contains(.say(.refused(code: "expired", message: "m", afterSAS: true))) && flow.screen == .closed,
                      "expired after a SAS was shown is the time to compare running out")
        flow = comparing()
        _ = send(.deadlinePassed(at: deadline.addingTimeInterval(3)))
        let still = send(.verbResult(.pairStatus, .done([.pair(.sasPending(name: pairName, sas: "1234-5678", deadline: deadline))])))
        report.expect(still.contains(.deadlineAt(deadline.addingTimeInterval(3 + Pairing.recheckDelay))),
                      "a SAS still pending past its deadline is asked about again later, not at once", "\(still)")

        checkPairingWalk(report)
    }

    /// Thousands of inputs, in a seeded order, partly following the gate and partly not, with
    /// some answers from an older epoch: the rules hold at every step.
    static func checkPairingWalk(_ report: Report) {
        let deadline = sampleTime
        let code = "swbp1_" + String(repeating: "Z", count: 120)
        let names = [pairName, "pair-other"]
        let sases = ["1234-5678", "8765-4321"]
        let deadlines = [deadline, deadline.addingTimeInterval(60)]
        var random = Seeded(state: 20_261_102)
        var flow = PairingFlow()
        var matched: PairingFlow.Comparison?
        var lastShown: PairingFlow.Comparison?
        var clock: TimeInterval = 0
        var violations: [String] = []
        var confirms = 0

        func randomPair() -> PairState {
            switch random.next(5) {
            case 0:
                return .waiting(name: random.pick(names))
            case 1:
                return .confirmed(name: random.pick(names))
            case 2:
                return .noPair
            default:
                return .sasPending(name: random.pick(names), sas: random.pick(sases), deadline: random.pick(deadlines))
            }
        }
        func randomStatusPair() -> StatusPair {
            switch random.next(6) {
            case 0:
                return .waiting(name: random.pick(names))
            case 1:
                return .confirmed(name: random.pick(names))
            case 2:
                return .expired(name: random.pick(names), deadline: random.pick(deadlines))
            case 3:
                return .noPair
            default:
                return .sasPending(name: random.pick(names), deadline: random.pick(deadlines))
            }
        }
        func randomOutcome() -> VerbOutcome {
            switch random.next(6) {
            case 0:
                return .done([.pair(randomPair())])
            case 1:
                return .done([.status(status(randomStatusPair()))])
            case 2:
                return .refused(HostError(code: random.pick(["waiting", "sas_ready", "expired", "held", "none", "room_not_left"]),
                                          message: "m", kind: nil))
            case 3:
                return .timedOut
            case 4:
                return .signaled
            default:
                return .done([.pair(.rejected(name: random.pick(names), lucilleTold: random.next(2) == 0))])
            }
        }
        func guided() -> PairingFlow.Input {
            switch flow.screen {
            case .closed:
                return .startConnecting
            case .enterCode:
                return .submit(code)
            case .waiting:
                if flow.pairChildAlive {
                    return .childObject(.pair(.sasPending(name: random.pick(names), sas: random.pick(sases), deadline: random.pick(deadlines))))
                }
                return .verbResult(.pairStatus, .done([.pair(randomPair())]))
            case .compare(let shown):
                if let drawnAt = shown.drawnAt {
                    return .theyMatch(at: drawnAt + 1)
                }
                return .sasDrawn(shown.sas, at: clock)
            case .busy(.checking):
                if let shown = lastShown, random.next(3) > 0 {
                    return .verbResult(random.pick([.status(.beforeConfirm), .status(.afterConfirm)]),
                                       .done([.status(status(.sasPending(name: shown.name, deadline: shown.deadline)))]))
                }
                return .verbResult(.status(.beforeConfirm), randomOutcome())
            case .busy(.confirming):
                return .verbResult(.pairConfirm, .done([.pair(.confirmed(name: random.pick(names)))]))
            case .busy(.rejecting):
                return .verbResult(.pairReject, randomOutcome())
            }
        }
        func noise() -> PairingFlow.Input {
            switch random.next(15) {
            case 0:
                return .startConnecting
            case 1:
                return .resume(randomStatusPair())
            case 2:
                return .submit(random.next(2) == 0 ? code : "not a code")
            case 3:
                return .childObject(.pair(randomPair()))
            case 4:
                return .childObject(.error(HostError(code: random.pick(["interrupted", "hub_error", "leaked", "held"]), message: "m", kind: nil)))
            case 5:
                return .childExited(random.pick([.exited(0), .exited(1), .signaled(15)]))
            case 6:
                return .verbResult(random.pick([.pairStatus, .pairConfirm, .pairReject, .status(.beforeConfirm),
                                                .status(.afterConfirm), .status(.afterChild)]), randomOutcome())
            case 7:
                return .sasDrawn(random.pick(sases), at: clock)
            case 8:
                return .theyMatch(at: clock + Double(random.next(3)))
            case 9:
                return .theyDiffer
            case 10:
                return .pollDue
            case 11:
                return .deadlinePassed(at: random.pick(deadlines).addingTimeInterval(3))
            case 12:
                return .windowClosed
            case 13:
                return .stopConnecting
            default:
                return .childTimeLimit
            }
        }

        for step in 0..<40_000 {
            clock += 0.2
            if step % 400 == 0 {
                // A relaunch: They Differ latches only four pairs and deadlines here, and a walk
                // that has latched them all can never reach pair confirm again.
                flow = PairingFlow()
                matched = nil
                lastShown = nil
            }
            let input = random.next(10) < 6 ? guided() : noise()
            let stale = random.next(10) == 0
            let epoch = stale ? flow.epoch - 1 : flow.epoch
            let screenBefore = flow.screen
            if case .compare(let shown) = screenBefore {
                lastShown = shown
            }
            let effects = flow.handle(input, epoch: epoch)
            if effects.contains(.run(.pairStatus)) {
                switch flow.screen {
                case .waiting, .compare:
                    break
                default:
                    violations.append("\(step): pair status with the window showing \(flow.screen)")
                }
            }
            if case .theyMatch = input, effects.contains(.run(.status(.beforeConfirm))), case .compare(let shown) = screenBefore {
                matched = shown
            }
            if effects.contains(.run(.pairConfirm)) {
                confirms += 1
                var bound = false
                if case .verbResult(.status(.beforeConfirm), let outcome) = input, !stale,
                   let found = outcome.status, case .sasPending(let name, let due) = found.pair,
                   let shown = matched, shown.name == name, shown.deadline == due, shown.drawn, shown.canMatch {
                    bound = true
                }
                if !bound {
                    violations.append("\(step): pair confirm after \(input)")
                }
                matched = nil
            }
            for effect in effects {
                if case .startPair = effect {
                    if case .submit = input {
                    } else {
                        violations.append("\(step): pair started by \(input)")
                    }
                }
            }
        }
        report.expect(violations.isEmpty, "over 40000 seeded steps, pair confirm follows only a drawn, matched, still-pending SAS, and pair status only an open window",
                      violations.prefix(5).joined(separator: "; "))
        report.expect(confirms >= 200, "the walk reached pair confirm often (\(confirms) times)")
    }

    // MARK: - checkSupervisor

    static func checkSupervisor(_ fixtures: [String: Fixture], _ report: Report) {
        report.expect((1...12).map { RunSupervisor.backoff(attempt: $0) } == [2, 4, 8, 16, 32, 64, 128, 256, 300, 300, 300, 300],
                      "the back-off is 2, 4, 8 … 256, then 300 s")

        let wall0 = Date(timeIntervalSince1970: 1_800_000_000)
        var sup = RunSupervisor()
        var clock: TimeInterval = 1000
        func send(_ input: RunSupervisor.Input, at time: TimeInterval? = nil) -> [RunSupervisor.Effect] {
            if let time {
                clock = time
            }
            return sup.handle(input, epoch: sup.epoch, now: clock, wall: wall0.addingTimeInterval(clock))
        }
        let confirmed = status(.confirmed(name: pairName))
        let running = status(.confirmed(name: pairName), run: .running(pid: 42))
        let stoppedRun = status(.confirmed(name: pairName), run: .stopped)
        func fresh() {
            sup = RunSupervisor()
            clock = 1000
        }
        func spawned() {
            fresh()
            _ = send(.start)
            _ = send(.statusRead(confirmed))
        }
        func finish(_ end: ChildEnd, _ stopped: StopReason?) -> [RunSupervisor.Effect] {
            if let stopped {
                _ = send(.object(.stopped(stopped)))
            }
            return send(.exited(ChildExit(end: end, stdoutComplete: true)))
        }

        fresh()
        report.expect(send(.start) == [.readStatus] && send(.statusRead(confirmed)) == [.spawnRun, .terminationGuard(true)],
                      "run starts only from a status read that says the pair is confirmed")
        fresh()
        _ = send(.start)
        report.expect(send(.statusRead(status(.sasPending(name: pairName, deadline: sampleTime)))).isEmpty && sup.phase == .idle,
                      "a pair not confirmed yet starts nothing")
        fresh()
        _ = send(.start)
        report.expect(send(.statusRead(status(.noPair))).isEmpty && sup.phase == .halted(.notConnected(.notPaired)),
                      "no pair: not connected")
        fresh()
        _ = send(.start)
        _ = send(.statusRead(status(.unreadable(code: "store_error"))))
        report.expect(sup.phase == .halted(.needsAttention(.storeError)) && Attention.storeError.offersTryAgain,
                      "a store status cannot read needs attention, with Try Again")
        fresh()
        _ = send(.start)
        _ = send(.statusFailed(.refused(HostError(code: "state_invalid", message: "m", kind: nil))))
        report.expect(sup.phase == .halted(.needsAttention(.statusFailed(code: "state_invalid")))
                          && send(.tryAgain) == [.readStatus] && send(.statusRead(confirmed)) == [.spawnRun, .terminationGuard(true)],
                      "a status that failed starts nothing until Try Again")

        // Crashes: the back-off, and five in ten minutes stop the restarts.
        spawned()
        var times: [TimeInterval] = []
        for _ in 0..<4 {
            _ = finish(.signaled(9), nil)
            guard case .backoff(let until) = sup.phase else {
                report.expect(false, "a crash backs off", "\(sup.phase)")
                break
            }
            times.append(until - clock)
            report.expect(send(.tick, at: until - 0.5).isEmpty, "nothing before the back-off ends")
            report.expect(send(.tick, at: until) == [.readStatus], "then status is read")
            _ = send(.statusRead(confirmed))
        }
        report.expect(times == [2, 4, 8, 16], "crashes 1 to 4 back off 2, 4, 8 and 16 s", "\(times)")
        _ = finish(.signaled(9), nil)
        report.expect(sup.phase == .halted(.needsAttention(.keepsStopping)), "the fifth crash in ten minutes stops the restarts", "\(sup.phase)")
        report.expect(send(.tryAgain) == [.readStatus] && sup.crashes.isEmpty && sup.attempt == 0,
                      "Try Again clears the count, and reads status")
        spawned()
        for _ in 0..<5 {
            _ = finish(.exited(1), .failed(code: "internal"))
            guard case .backoff = sup.phase else {
                break
            }
            _ = send(.tick, at: clock + 200)
            _ = send(.statusRead(confirmed))
        }
        report.expect(sup.runAlive, "five crashes spread over more than ten minutes do not stop it", "\(sup.phase)")
        spawned()
        _ = send(.object(.ready(pair: pairName, hostVersion: "1")))
        _ = finish(.exited(1), .failed(code: "store_error"))
        report.expect(sup.crashes.count == 1 && sup.phase == .backoff(until: clock + 2),
                      "a failed store_error after ready counts as a crash", "\(sup.phase)")
        _ = send(.tick, at: clock + 2)
        _ = send(.statusRead(confirmed))
        _ = finish(.signaled(9), nil)
        report.expect(sup.phase == .backoff(until: clock + 4), "the next crash backs off longer", "\(sup.phase)")
        _ = send(.tick, at: clock + 4)
        _ = send(.statusRead(confirmed))
        _ = send(.object(.ready(pair: pairName, hostVersion: "1")))
        _ = send(.tick, at: clock + 601)
        _ = finish(.signaled(9), nil)
        report.expect(sup.phase == .backoff(until: clock + 2), "ten minutes ready start the back-off from 2 s again", "\(sup.phase)")

        // Exits (spec/CLI.md §13.3): every run-events fixture, by its exit code and reason.
        let table: [String: RunSupervisor.Outcome] = [
            "run-events-already-running": .otherRun,
            "run-events-downgrade": .needsAttention(.downgrade(lastVersion: "999.0.0")),
            "run-events-drained": .crash,
            "run-events-ended": .notConnected(.ended),
            "run-events-expired": .notConnected(.expired),
            "run-events-failed": .crash,
            "run-events-insecure-keyring": .needsAttention(.insecureKeyring),
            "run-events-leaked": .notConnected(.leaked),
            "run-events-no-gate": .notConnected(.noGate),
            "run-events-not-paired": .notConnected(.notPaired),
            "run-events-pair-unusable": .needsAttention(.pairUnusable),
            "run-events-requests": .crash,
            "run-events-several-pairs": .needsAttention(.severalPairs),
            "run-events-startup-refused": .needsAttention(.startupRefused),
            "run-events-store-error": .needsAttention(.storeError),
            "run-json": .needsAttention(.appBug),
        ]
        for (name, fixture) in fixtures.sorted(by: { $0.key < $1.key }) where name.hasPrefix("run-events-") || name == "run-json" {
            let got = RunSupervisor.classify(exit: .exited(fixture.exit), stopped: fixture.stopped, asked: false)
            report.expect(table[name] != nil && got == table[name], "\(name) (exit \(fixture.exit)) maps to its outcome", "\(got)")
        }
        if !fixtures.isEmpty {
            report.expect(Set(table.keys).isSubset(of: Set(fixtures.keys)), "every outcome in the table has its fixture")
        }
        let drained = StopReason.drained(cause: .stdinEnd)
        report.expect(RunSupervisor.classify(exit: .exited(0), stopped: drained, asked: true) == .drainedAsAsked
                          && RunSupervisor.classify(exit: .exited(0), stopped: drained, asked: false) == .crash,
                      "a drain the app asked for is done; one it did not ask for is a crash")
        report.expect(RunSupervisor.classify(exit: .exited(0), stopped: .drained(cause: .other("moon")), asked: true) == .crash,
                      "a drain cause this release does not know is a drain it did not ask for")
        report.expect(RunSupervisor.classify(exit: .exited(24), stopped: .startupRefused, asked: false) == .needsAttention(.startupRefused)
                          && RunSupervisor.classify(exit: .signaled(24), stopped: .startupRefused, asked: false) == .crash
                          && RunSupervisor.classify(exit: .signaled(20), stopped: .notPaired, asked: false) == .crash,
                      "a signal is a crash, whatever its number and whatever run said")
        report.expect(RunSupervisor.classify(exit: .exited(20), stopped: drained, asked: false) == .crash
                          && RunSupervisor.classify(exit: .exited(0), stopped: .notPaired, asked: false) == .crash,
                      "an exit code that disagrees with the reason is a crash")
        report.expect(RunSupervisor.classify(exit: .exited(0), stopped: nil, asked: false) == .askStatus
                          && RunSupervisor.classify(exit: .exited(20), stopped: nil, asked: false) == .askStatus,
                      "no stopped after 0 or 20: status first")
        report.expect(RunSupervisor.classify(exit: .exited(20), stopped: .other("new_reason"), asked: false) == .notConnected(.other)
                          && RunSupervisor.classify(exit: .exited(23), stopped: .other("new_reason"), asked: false) == .needsAttention(.storeError)
                          && RunSupervisor.classify(exit: .exited(7), stopped: nil, asked: false) == .crash
                          && RunSupervisor.classify(exit: .exited(2), stopped: nil, asked: false) == .needsAttention(.appBug),
                      "a reason this release does not know goes by the exit code; an exit code it does not know is a crash")
        report.expect(RunSupervisor.describe(.exited(0), stopped: drained) == "run exited 0 (drained, stdin_end)"
                          && RunSupervisor.describe(.signaled(9), stopped: nil) == "run ended by signal 9 (no stopped object)",
                      "each exit has one diagnostic line")

        spawned()
        report.expect(finish(.exited(0), nil).contains(.readStatus) && sup.phase == .awaitingStatus(.afterExit),
                      "an exit 0 with no stopped reads status")
        _ = send(.statusRead(confirmed))
        report.expect(sup.phase == .backoff(until: clock + 2), "and, still confirmed, it was a crash")
        spawned()
        _ = finish(.exited(20), nil)
        _ = send(.statusRead(status(.noPair)))
        report.expect(sup.phase == .halted(.notConnected(.notPaired)), "an exit 20 with no stopped and no pair: not connected")

        // Exit 21: wait while a run from before finishes, then start; past 330 s, another copy.
        spawned()
        report.expect(finish(.exited(21), .alreadyRunning).contains(.readStatus), "exit 21 reads status")
        _ = send(.statusRead(running))
        if case .otherRun(_, let retryAt) = sup.phase {
            report.expect(sup.health(now: clock, wall: wall0, status: running) == .finishingEarlier,
                          "the menu says a task from before is finishing")
            report.expect(send(.tick, at: retryAt) == [.readStatus], "status is read again after the back-off")
            report.expect(send(.statusRead(stoppedRun)) == [.spawnRun, .terminationGuard(true)], "once it stopped, run starts at once")
        } else {
            report.expect(false, "a run still running is waited for", "\(sup.phase)")
        }
        spawned()
        _ = finish(.exited(21), .alreadyRunning)
        _ = send(.statusRead(running), at: clock + 331)
        report.expect(sup.phase == .halted(.needsAttention(.anotherCopy)), "past 330 s, another copy is running", "\(sup.phase)")

        // An object of another version: close the lifeline, then needs attention.
        spawned()
        report.expect(send(.object(.otherVersion(2))) == [.closeLifeline] && send(.object(.otherVersion(2))).isEmpty,
                      "an object of another version closes the lifeline, once")
        _ = finish(.exited(0), drained)
        report.expect(sup.phase == .halted(.needsAttention(.contract)), "and the exit then needs attention")

        // The quit ladder: panel at 0.5 s, kill at 330 s, SIGKILL 40 s later; logout kills at 20 s.
        spawned()
        let quitAt = clock
        report.expect(send(.quit(.quit)) == [.closeLifeline] && sup.nextWake == quitAt + 0.5, "Quit closes the lifeline")
        report.expect(send(.tick, at: quitAt + 0.5) == [.showFinishing(logout: false)] && sup.nextWake == quitAt + 330,
                      "the finishing panel shows after half a second")
        report.expect(send(.tick, at: quitAt + 329).isEmpty, "nothing more before 330 s")
        report.expect(send(.tick, at: quitAt + 330) == [.runKill, .finishingStopping] && sup.nextWake == quitAt + 370,
                      "at 330 s, kill, which pauses")
        report.expect(send(.tick, at: quitAt + 370) == [.sigkillRun] && sup.nextWake == nil, "40 s later, SIGKILL")
        let quitExit = send(.exited(ChildExit(end: .signaled(9), stdoutComplete: true)))
        report.expect(quitExit == [.terminationGuard(false), .log("run ended by signal 9 (no stopped object)"), .hideFinishing, .replyTerminate]
                          && sup.phase == .halted(.quitDone),
                      "then the app may quit", "\(quitExit)")
        spawned()
        let logoutAt = clock
        _ = send(.quit(.logout))
        _ = send(.tick, at: logoutAt + 0.5)
        report.expect(send(.tick, at: logoutAt + 20) == [.runKill, .finishingStopping], "a logout kills at 20 s")
        spawned()
        _ = send(.quit(.quit))
        _ = send(.tick, at: clock + 0.5)
        report.expect(send(.stopNow, at: clock + 5) == [.runKill, .finishingStopping] && sup.nextWake == clock + 40,
                      "Stop now kills at once, and SIGKILL follows 40 s later")
        spawned()
        _ = send(.quit(.quit))
        _ = send(.tick, at: clock + 0.5)
        _ = send(.object(.stopped(drained)), at: clock + 3)
        report.expect(sup.nextWake == clock + 10 && send(.tick, at: clock + 10).contains(.sigkillRun),
                      "a run that said stopped and has not exited 10 s later gets SIGKILL")
        spawned()
        _ = send(.quit(.quit))
        report.expect(send(.quit(.logout)).isEmpty && send(.tick, at: clock + 20).contains(.runKill),
                      "a logout during a quit takes the logout's bound")
        spawned()
        let drainExit = finish(.exited(0), drained)
        report.expect(drainExit.contains(.readStatus) == false && sup.phase == .backoff(until: clock + 2),
                      "a drain the app did not ask for is a crash")
        fresh()
        report.expect(send(.quit(.quit)) == [.replyTerminate], "a quit with no run lets the app quit at once")
        spawned()
        _ = send(.object(.stopped(.notPaired)))
        report.expect(send(.tick, at: clock + 10) == [.sigkillRun], "a run that said stopped and did not exit is killed")
        _ = send(.exited(ChildExit(end: .signaled(9), stdoutComplete: true)))
        report.expect(sup.phase == .halted(.notConnected(.notPaired)), "and its exit goes by what it said")

        // Disconnect drains first, then forgets; a pairing or a Disconnect drops pending restarts.
        spawned()
        report.expect(send(.disconnect) == [.closeLifeline], "Disconnect drains run first")
        report.expect(finish(.exited(0), drained).contains(.runForget) && sup.phase == .idle, "then runs forget")
        fresh()
        report.expect(send(.disconnect) == [.runForget], "with no run, it forgets at once")
        for cancel in [RunSupervisor.Input.pairingStarted, .disconnect] {
            spawned()
            _ = finish(.signaled(9), nil)
            guard case .backoff(let until) = sup.phase else {
                report.expect(false, "a crash backs off")
                continue
            }
            let oldEpoch = sup.epoch
            _ = send(cancel)
            report.expect(send(.tick, at: until + 1).isEmpty
                              && sup.handle(.statusRead(confirmed), epoch: oldEpoch, now: clock, wall: wall0).isEmpty,
                          "\(cancel) drops the pending restart, and a status read from before it", "\(sup.phase)")
        }

        // A status read from an earlier epoch is dropped while a fresh one is awaited, where only
        // the epoch tells them apart.
        fresh()
        _ = send(.start)
        let firstEpoch = sup.epoch
        _ = send(.pairingStarted)
        _ = send(.start)
        let lateRead = sup.handle(.statusRead(confirmed), epoch: firstEpoch, now: clock, wall: wall0.addingTimeInterval(clock))
        let lateFail = sup.handle(.statusFailed(.timedOut), epoch: firstEpoch, now: clock, wall: wall0.addingTimeInterval(clock))
        report.expect(lateRead.isEmpty && lateFail.isEmpty && sup.phase == .awaitingStatus(.start),
                      "a status read, or one that failed, from an earlier epoch changes nothing", "\(sup.phase)")
        report.expect(send(.statusRead(confirmed)) == [.spawnRun, .terminationGuard(true)], "and the read asked for now starts run")

        // A quit while a Disconnect drains: the Disconnect is finished, forget first, then the reply.
        for kind in [RunSupervisor.DrainKind.quit, .logout] {
            spawned()
            _ = send(.disconnect)
            report.expect(send(.quit(kind)).isEmpty, "a \(kind) while Disconnect drains starts nothing more")
            let after = finish(.exited(0), drained)
            let forgets = after.firstIndex(of: .runForget)
            let replies = after.firstIndex(of: .replyTerminate)
            report.expect(forgets != nil && replies != nil && (forgets ?? 0) < (replies ?? 0) && sup.phase == .halted(.quitDone),
                          "a \(kind) during a Disconnect still forgets, before the app replies", "\(after)")
        }
        spawned()
        _ = send(.disconnect)
        _ = send(.quit(.logout))
        report.expect(send(.tick, at: clock + 20).contains(.runKill), "a logout during a Disconnect takes the logout's bound")

        // A halt that needs the user stays until Try Again: neither a start nor a pairing lifts it.
        spawned()
        while sup.phase != .halted(.needsAttention(.keepsStopping)) && sup.crashes.count < 6 {
            _ = finish(.signaled(9), nil)
            if case .backoff(let until) = sup.phase {
                _ = send(.tick, at: until)
                _ = send(.statusRead(confirmed))
            }
        }
        let keepsStopping = RunSupervisor.Phase.halted(.needsAttention(.keepsStopping))
        report.expect(sup.phase == keepsStopping && send(.start).isEmpty && sup.phase == keepsStopping
                          && send(.pairingStarted).isEmpty && sup.phase == keepsStopping,
                      "five crashes stay halted through a start and a pairing", "\(sup.phase)")

        // The back-off starts from 2 s again after ten minutes of run being ready, not of it running.
        spawned()
        let crashBase = clock
        _ = finish(.signaled(9), nil)
        _ = send(.tick, at: crashBase + 2)
        _ = send(.statusRead(confirmed))
        _ = send(.object(.ready(pair: pairName, hostVersion: "1")), at: crashBase + 102)
        _ = send(.exited(ChildExit(end: .signaled(9), stdoutComplete: true)), at: crashBase + 652)
        report.expect(sup.phase == .backoff(until: crashBase + 656),
                      "a run started 650 s before its crash, but ready for 550 s, keeps its back-off", "\(sup.phase)")

        // host_too_old: once, for the highest version asked.
        spawned()
        let told = [("2.0.0", true), ("1.5.0", false), ("2.0.0", false), ("10.0.0", true)].allSatisfy { least, tells in
            send(.object(.hostTooOld(jobId: "j", minVersion: least, hostVersion: "1.0.0"))).isEmpty != tells
        }
        report.expect(told, "Lucille asking for a newer release is said once for the highest version")
        // A Lucille that raises its minimum on every request: the supervisor tells the glue once for
        // each, and keeps the highest, which the one keyed notice reads when it is drawn. A lower
        // minimum after them tells nothing.
        spawned()
        var tellings = 0
        for number in 1...50 {
            tellings += send(.object(.hostTooOld(jobId: "j\(number)", minVersion: "\(number).0.0", hostVersion: "0.1.0.dev0"))).count
        }
        let lower = send(.object(.hostTooOld(jobId: "late", minVersion: "49.9.9", hostVersion: "0.1.0.dev0")))
        report.expect(tellings == 50 && lower.isEmpty && sup.highestMinVersion == "50.0.0",
                      "fifty host_too_old objects of rising versions keep the highest, 50.0.0",
                      "\(tellings) \(String(describing: sup.highestMinVersion)) \(lower)")

        // Stop What's Running (decisions/0002 §14): `kill`, which also pauses, only while run runs,
        // as an effect of its own; it changes nothing in the supervisor, and resumes nothing.
        spawned()
        let runningPhase = sup.phase
        report.expect(send(.userStop) == [.stopWhatsRunning] && sup.phase == runningPhase,
                      "Stop What's Running, while run runs, asks for kill and nothing else", "\(sup.phase)")
        fresh()
        report.expect(send(.userStop).isEmpty && sup.phase == .idle, "with no run, Stop What's Running does nothing")
        _ = send(.start)
        report.expect(send(.userStop).isEmpty && sup.phase == .awaitingStatus(.start), "nor while status is read")
        spawned()
        _ = send(.quit(.quit))
        let drainingPhase = sup.phase
        var drains = false
        if case .draining = drainingPhase {
            drains = true
        }
        report.expect(drains && send(.userStop).isEmpty && sup.phase == drainingPhase, "nor while run drains", "\(drainingPhase)")
        spawned()
        _ = finish(.signaled(9), nil)
        let backoffPhase = sup.phase
        var waits = false
        if case .backoff = backoffPhase {
            waits = true
        }
        report.expect(waits && send(.userStop).isEmpty && sup.phase == backoffPhase, "nor during a back-off", "\(backoffPhase)")
        fresh()
        _ = send(.start)
        _ = send(.statusRead(status(.noPair)))
        report.expect(send(.userStop).isEmpty && sup.phase == .halted(.notConnected(.notPaired)), "nor once halted")

        checkHealth(report)
        checkSupervisorWalk(report)
    }

    static func checkHealth(_ report: Report) {
        let wall0 = Date(timeIntervalSince1970: 1_800_000_000)
        var sup = RunSupervisor()
        var clock: TimeInterval = 1000
        // How far the wall clock is ahead of `clock`, as after a sleep.
        var jump: TimeInterval = 0
        func send(_ input: RunSupervisor.Input, at time: TimeInterval? = nil) {
            if let time {
                clock = time
            }
            _ = sup.handle(input, epoch: sup.epoch, now: clock, wall: wall0.addingTimeInterval(clock + jump))
        }
        func makeState(
            confirmed: Bool = true, policy: RunState.Policy = .ok, clock skew: ClockState = .ok(skew: 0),
            contact: Date? = nil, rooms: [RunRoom] = [RunRoom(name: "room-1", state: .reading)], left: String? = nil,
            paused: Bool = false
        ) -> RunState {
            RunState(pair: pairName, confirmed: confirmed, paused: paused, policy: policy, lastHubContact: contact,
                     clock: skew, rooms: rooms, offersWaiting: 0, lastLeftBecause: left)
        }
        func currentHealth() -> Health {
            sup.health(now: clock, wall: wall0.addingTimeInterval(clock + jump), status: nil)
        }
        func ready() {
            sup = RunSupervisor()
            clock = 1000
            jump = 0
            send(.start)
            send(.statusRead(status(.confirmed(name: pairName))))
            report.expect(currentHealth() == .connecting, "a run not ready yet is connecting")
            send(.object(.ready(pair: pairName, hostVersion: "1")))
        }
        func contact() -> Date {
            wall0.addingTimeInterval(clock + jump)
        }
        ready()
        send(.object(.state(makeState(contact: contact()))))
        report.expect(currentHealth() == .connected, "a room reading: connected")
        send(.object(.state(makeState(contact: contact(), rooms: []))))
        report.expect(currentHealth() == .connectedWaiting, "no room yet: connected, waiting for Lucille")
        send(.object(.state(makeState(confirmed: false, contact: contact(), rooms: []))))
        report.expect(currentHealth() == .connecting, "the gate not open: connecting")
        send(.object(.state(makeState(contact: contact(), rooms: [], left: "check_3"))))
        report.expect(currentHealth() == .needsAttention(.leftConnection(because: "check_3")), "a room left on its own needs attention")
        send(.object(.state(makeState(contact: contact(), rooms: [RunRoom(name: "room-1", state: .readStuck)]))))
        report.expect(currentHealth() == .needsAttention(.readStuck), "a room that cannot be read needs attention")
        send(.object(.state(makeState(contact: wall0.addingTimeInterval(clock - 121)))))
        report.expect(currentHealth() == .needsAttention(.hubUnreachable(since: wall0.addingTimeInterval(clock - 121))),
                      "no hub contact for over 120 s: out of reach, since then")
        send(.object(.state(makeState(clock: .skewed(skew: 600), contact: wall0.addingTimeInterval(clock - 121)))))
        report.expect(currentHealth() == .needsAttention(.clock(skew: 600)), "a skewed clock comes before the hub")
        send(.object(.state(makeState(policy: .checking, clock: .skewed(skew: 600), contact: contact()))), at: clock + 1)
        report.expect(currentHealth() == .needsAttention(.clock(skew: 600)), "checking for under 30 s is no problem")
        send(.tick, at: clock + 31)
        report.expect(currentHealth() == .needsAttention(.policyStuck), "checking for over 30 s comes before the clock")
        send(.object(.state(makeState(policy: .invalid, clock: .skewed(skew: 600)))))
        report.expect(currentHealth() == .needsAttention(.policyInvalid), "an invalid policy comes before the clock")
        send(.tick, at: clock + 120)
        report.expect(currentHealth() == .needsAttention(.notResponding), "no state for 120 s: not responding, before the rest")
        ready()
        let contacted = contact()
        send(.object(.state(makeState(contact: contacted))))
        jump = 3600
        send(.tick, at: clock + 10)
        report.expect(currentHealth() == .connected && sup.graceUntil == clock + RunSupervisor.wakeGrace,
                      "after a wake, a hub contact an hour old by the wall clock is not yet a problem")
        send(.object(.state(makeState(contact: contacted))), at: clock + RunSupervisor.wakeGrace + 1)
        report.expect(currentHealth() == .needsAttention(.hubUnreachable(since: contacted)),
                      "once the grace has passed, it is", "\(currentHealth())")
        ready()
        send(.object(.state(makeState(contact: contact(), paused: true))))
        report.expect(sup.paused(status: status(.confirmed(name: pairName), paused: false)), "while run runs, its state says whether paused")
        report.expect(RunSupervisor().paused(status: status(.noPair, paused: true)), "otherwise status does")
        report.expect(RunSupervisor.health(of: nil) == .notConnected(nil)
                          && RunSupervisor.health(of: status(.noPair)) == .notConnected(nil)
                          && RunSupervisor.health(of: status(.waiting(name: pairName))) == .connecting
                          && RunSupervisor.health(of: status(.expired(name: pairName, deadline: sampleTime))) == .notConnected(.expired)
                          && RunSupervisor.health(of: status(.severalPairs)) == .needsAttention(.halted(.severalPairs))
                          && RunSupervisor.health(of: status(.unreadable(code: "insecure_keyring"))) == .needsAttention(.halted(.insecureKeyring)),
                      "with no run, the status line comes from status")
        for problem in [Problem.notResponding, .policyInvalid, .policyStuck, .clock(skew: nil), .hubUnreachable(since: nil),
                        .hubUnreachable(since: sampleTime), .readStuck, .leftConnection(because: "check_4"),
                        .leftConnection(because: "left_before"), .leftConnection(because: "something_new"),
                        .halted(.keepsStopping), .halted(.anotherCopy), .halted(.downgrade(lastVersion: "9.9.9")),
                        .halted(.statusFailed(code: nil)), .halted(.statusFailed(code: "state_invalid"))] {
            report.expect(!ConnectionText.problem(problem).isEmpty
                              && ConnectionText.reason(.needsAttention(problem)) == ConnectionText.problem(problem),
                          "\(problem) has words under Needs attention")
        }
        for why in [NotConnected.noGate, .leaked, .expired, .ended, .forgotten] {
            report.expect(ConnectionText.notConnectedReason(why) != nil, "not connected because \(why) has words")
        }
    }

    /// Thousands of inputs in a seeded order: run starts only from a confirmed status read it
    /// asked for, resume runs only when the user asks, and what's running is stopped only when
    /// the user asks, while run runs.
    static func checkSupervisorWalk(_ report: Report) {
        var random = Seeded(state: 9_202_611)
        var sup = RunSupervisor()
        var clock: TimeInterval = 1000
        var violations: [String] = []
        var spawns = 0
        var stops = 0
        let wall0 = Date(timeIntervalSince1970: 1_800_000_000)
        let pairs: [StatusPair] = [.confirmed(name: pairName), .noPair, .waiting(name: pairName),
                                   .unreadable(code: "store_error"), .severalPairs]
        let runs: [StatusRun] = [.running(pid: 1), .stopped, .notSeen(pid: 1), .noRecord]
        let reasons: [StopReason?] = [nil, .drained(cause: .stdinEnd), .notPaired, .alreadyRunning, .failed(code: "x"),
                                      .storeError, .forgotten, .other("new")]
        for step in 0..<40_000 {
            clock += Double(random.next(40))
            let input: RunSupervisor.Input
            switch random.next(13) {
            case 0:
                input = .start
            case 1, 2:
                input = .statusRead(status(random.pick(pairs), run: random.pick(runs)))
            case 3:
                input = .statusFailed(.timedOut)
            case 4:
                input = .object(random.pick([.ready(pair: pairName, hostVersion: "1"), .otherVersion(2),
                                             .state(RunState(pair: pairName, confirmed: true, paused: false, policy: .ok,
                                                             lastHubContact: nil, clock: .unknown, rooms: [], offersWaiting: 0,
                                                             lastLeftBecause: nil))]))
            case 5:
                if let reason = random.pick(reasons) {
                    input = .object(.stopped(reason))
                } else {
                    input = .tick
                }
            case 6:
                input = .exited(ChildExit(end: random.pick([.exited(0), .exited(1), .exited(20), .exited(21), .signaled(9)]),
                                          stdoutComplete: true))
            case 7:
                input = .tick
            case 8:
                input = .quit(random.pick([.quit, .logout]))
            case 9:
                input = random.pick([.stopNow, .disconnect, .spawnFailed, .userStop])
            case 10:
                input = .pairingStarted
            case 11:
                input = .tryAgain
            default:
                input = random.next(4) == 0 ? .userResume : .tick
            }
            let stale = random.next(8) == 0
            let epoch = stale ? sup.epoch - 1 : sup.epoch
            let phaseBefore = sup.phase
            let effects = sup.handle(input, epoch: epoch, now: clock, wall: wall0.addingTimeInterval(clock))
            if effects.contains(.spawnRun) {
                spawns += 1
                var fromConfirmed = false
                if case .statusRead(let found) = input, !stale, case .confirmed = found.pair, case .awaitingStatus = phaseBefore {
                    fromConfirmed = true
                }
                if !fromConfirmed {
                    violations.append("\(step): run started by \(input) in \(phaseBefore)")
                }
            }
            let resumes = effects.contains(.runResume)
            var asked = false
            if case .userResume = input {
                asked = true
            }
            if resumes != asked {
                violations.append("\(step): resume \(resumes) on \(input)")
            }
            var stopAsked = false
            if case .userStop = input, case .running = phaseBefore {
                stopAsked = true
            }
            if effects.contains(.stopWhatsRunning) {
                stops += 1
            }
            if effects.contains(.stopWhatsRunning) != stopAsked || (stopAsked && effects != [.stopWhatsRunning]) {
                violations.append("\(step): stop what's running \(effects) on \(input) in \(phaseBefore)")
            }
            if case .pairingStarted = input {
                switch sup.phase {
                case .backoff, .otherRun, .awaitingStatus:
                    violations.append("\(step): a pairing left a restart pending")
                default:
                    break
                }
            }
            // The quit after a halt starts over, so the walk keeps going.
            if case .halted(.quitDone) = sup.phase, random.next(4) == 0 {
                sup = RunSupervisor()
            }
        }
        report.expect(violations.isEmpty,
                      "over 40000 seeded steps, run starts only from a confirmed status read, resumes only when asked, and stops what's running only when asked while run runs",
                      violations.prefix(5).joined(separator: "; "))
        report.expect(spawns > 0, "the walk started run (\(spawns) times)")
        report.expect(stops > 0, "the walk stopped what's running (\(stops) times)")
    }

    // MARK: - checkSpawnLatch

    /// A copy anywhere but /Applications/<name>.app starts nothing (decisions/0002 §13): every
    /// child goes through `HostCLI.spawn`, which refuses until the latch is open, and only a token
    /// `Placement.judge` gives a placed copy opens it. Before that, a short verb is not started and
    /// a long-lived child refuses to start, and the fake interpreter, which leaves a file whenever
    /// it runs, never ran. Then a synthetic placed copy's token opens the latch, and the same verb
    /// runs: the control, and what every later check needs.
    static func checkSpawnLatch(home: String, _ report: Report) {
        let folder = home + "/latch"
        try? FileManager.default.createDirectory(atPath: folder, withIntermediateDirectories: true)
        let script = folder + "/fake-host"
        let marker = folder + "/ran"
        let text = """
            #!/bin/sh
            : > "$HOME/ran"
            exit 0

            """
        guard FileManager.default.createFile(atPath: script, contents: Data(text.utf8),
                                             attributes: [.posixPermissions: 0o755])
        else {
            report.expect(false, "write the fake interpreter at \(script)")
            return
        }
        let fake = HostCLI(python: URL(fileURLWithPath: script), home: folder)
        let shut = fake.run(["--json", "status"])
        report.expect(shut.status == CLIResult.notStarted && !shut.succeeded
                          && !FileManager.default.fileExists(atPath: marker),
                      "before the latch is open, a short verb is not started, and nothing runs", "\(shut)")
        let child = HostChild(cli: fake, verb: ["run", "--json-events", "--exit-on-stdin-eof"], input: .lifeline,
                              deliverOn: DispatchQueue(label: "selftest.latch"))
        var refusal: Error?
        do {
            try child.start()
        } catch {
            refusal = error
        }
        report.expect((refusal as? HostCLI.SpawnError) == .notPlaced && child.pid == 0
                          && !FileManager.default.fileExists(atPath: marker),
                      "nor does a long-lived child start", "\(String(describing: refusal))")
        guard case .placed(let token) = Placement.judge(
            bundlePath: "/Applications/Example.app", resolvedPath: "/Applications/Example.app",
            appName: "Example", home: "/Users/someone", originalPresent: false
        ) else {
            report.expect(false, "a synthetic copy at /Applications/Example.app is placed, and gets a token")
            return
        }
        HostCLI.allowSpawns(token)
        let opened = fake.run(["--json", "status"])
        report.expect(opened.succeeded && FileManager.default.fileExists(atPath: marker),
                      "once a placed copy's token opens the latch, the same verb runs", "\(opened)")
    }

    // MARK: - checkChild

    /// The child runner against fake children: a shell script in place of the interpreter, which
    /// prints chosen lines and ends as told. HostCLI starts any executable, with the app's fixed
    /// arguments first, which the script skips.
    static func checkChild(home: String, _ report: Report) {
        let script = home + "/fake-host"
        let text = """
            #!/bin/sh
            # A stand-in for the interpreter: -I -B -X utf8 -m lucille_host --profile NAME come first.
            shift 8
            printf '%s\\n' "$@" > "$HOME/argv"
            case "$1" in
            replay)
                /bin/cat "$2"
                exit "$3"
                ;;
            stderr)
                /bin/cat "$2" >&2
                exit "$3"
                ;;
            signal)
                /bin/cat "$2"
                kill -s "$3" $$
                /bin/sleep 5
                exit 99
                ;;
            lifeline)
                /bin/cat "$2"
                /bin/cat > /dev/null
                /bin/cat "$3"
                exit 0
                ;;
            stdin)
                /usr/bin/head -n 1 > "$HOME/stdin"
                /bin/cat "$2"
                exit "$3"
                ;;
            trap)
                trap '/bin/cat "$3"; exit 1' INT
                /bin/cat "$2"
                while :; do /bin/sleep 1; done
                ;;
            sleep)
                exec /bin/sleep "$2"
                ;;
            esac
            exit 98

            """
        guard FileManager.default.createFile(atPath: script, contents: Data(text.utf8),
                                             attributes: [.posixPermissions: 0o755])
        else {
            report.expect(false, "write the fake child at \(script)")
            return
        }
        func file(_ name: String, _ content: String) -> String {
            let path = home + "/" + name
            _ = FileManager.default.createFile(atPath: path, contents: Data(content.utf8))
            return path
        }
        let fake = HostCLI(python: URL(fileURLWithPath: script), home: home)
        let readyLine = #"{"v":1,"type":"ready","host_version":"1.0.0","pair":"pair-abcdef"}"#
        let stateLine = #"{"v":1,"type":"state","pair":"pair-abcdef","confirmed":true,"paused":false,"policy":"ok","last_hub_contact":null,"clock":{"state":"unknown"},"rooms":[],"offers_waiting":0,"last_left_because":null}"#
        let stoppedLine = #"{"v":1,"type":"stopped","reason":"drained","cause":"stdin_end"}"#
        let heldLine = #"{"v":1,"type":"error","code":"held","message":"this computer already holds a pair"}"#
        let pairCodeLine = #"{"v":1,"type":"pair_code","name":"pair-abcdef","hub":"h","note":""}"#
        let interruptedLine = #"{"v":1,"type":"error","code":"interrupted","message":"m"}"#

        func start(_ verb: [String], _ input: HostChild.Input = .lifeline) -> (HostChild, ChildWatch)? {
            let watch = ChildWatch()
            let child = HostChild(cli: fake, verb: verb, input: input, deliverOn: watch.queue)
            watch.watch(child)
            do {
                try child.start()
            } catch {
                report.expect(false, "start the fake child \(verb)", "\(error)")
                return nil
            }
            return (child, watch)
        }

        // Replay: the objects in order, then the exit, read before the number.
        let replay = file("replay.jsonl", [readyLine, stateLine, stoppedLine].joined(separator: "\n") + "\n")
        if let (_, watch) = start(["replay", replay, "0"]) {
            let ended = watch.waitForExit(seconds: 10)
            report.expect(ended == ChildExit(end: .exited(0), stdoutComplete: true)
                              && watch.objects == [.ready(pair: pairName, hostVersion: "1.0.0"),
                                                   HostObject.state(RunState(pair: pairName, confirmed: true, paused: false, policy: .ok,
                                                                             lastHubContact: nil, clock: .unknown, rooms: [],
                                                                             offersWaiting: 0, lastLeftBecause: nil)),
                                                   .stopped(.drained(cause: .stdinEnd))],
                          "a child's objects arrive in order, then its exit", "\(String(describing: ended)) \(watch.objects)")
        }
        let already = file("already.jsonl", #"{"v":1,"type":"stopped","reason":"already_running"}"# + "\n")
        if let (_, watch) = start(["replay", already, "21"]) {
            report.expect(watch.waitForExit(seconds: 10)?.end == .exited(21), "exit 21 is an exit")
        }

        // A signal is read before the number: SIGXCPU is 24, as is the exit code of startup_refused.
        let readyFile = file("ready.jsonl", readyLine + "\n")
        if let (_, watch) = start(["signal", readyFile, "XCPU"]) {
            let ended = watch.waitForExit(seconds: 10)
            report.expect(ended?.end == .signaled(24), "a child ended by SIGXCPU reads as signal 24, not exit 24", "\(String(describing: ended))")
            if let ended {
                report.expect(RunSupervisor.classify(exit: ended.end, stopped: .startupRefused, asked: false) == .crash,
                              "and is a crash")
            }
        }
        let short = fake.run(["signal", readyFile, "XCPU"])
        report.expect(short.terminationReason == .uncaughtSignal && short.signal == 24 && !short.succeeded
                          && VerbOutcome(short) == .signaled,
                      "a short run ended by a signal says so", "\(short)")

        // The lifeline: closing it drains the child while another child, started after it, lives.
        let stoppedFile = file("stopped.jsonl", stoppedLine + "\n")
        if let (child, watch) = start(["lifeline", readyFile, stoppedFile]) {
            report.expect(watch.wait(seconds: 10) { watch.objects.contains(.ready(pair: pairName, hostVersion: "1.0.0")) },
                          "the fake run is ready")
            let token = CancelToken()
            let sleeperDone = DispatchSemaphore(value: 0)
            DispatchQueue.global().async {
                _ = fake.run(["sleep", "30"], cancel: token, timeout: 60)
                sleeperDone.signal()
            }
            Thread.sleep(forTimeInterval: 1)
            report.expect(child.isRunning && child.pid > 0, "the fake run runs")
            child.closeLifeline()
            let ended = watch.waitForExit(seconds: 5)
            report.expect(ended?.end == .exited(0) && watch.objects.last == .stopped(.drained(cause: .stdinEnd)),
                          "closing the lifeline ends the fake run within 5 s, so no other child holds its write end",
                          "\(String(describing: ended))")
            report.expect(sleeperDone.wait(timeout: .now()) == .timedOut, "while the child started after it still runs")
            token.cancel()
            report.expect(sleeperDone.wait(timeout: .now() + 20) == .success, "and that child is stopped")
            report.expect(!child.isRunning, "an ended child is not running")
            child.interrupt()
            child.killNow()
        }

        // The code goes on standard input, never in argv.
        let code = "swbp1_" + String(repeating: "Kq9_-", count: 60)
        let heldFile = file("held.jsonl", heldLine + "\n")
        if let (_, watch) = start(["stdin", heldFile, "1"], .code(code)) {
            let ended = watch.waitForExit(seconds: 10)
            let read = FileManager.default.contents(atPath: home + "/stdin").map { String(decoding: $0, as: UTF8.self) }
            let argv = FileManager.default.contents(atPath: home + "/argv").map { String(decoding: $0, as: UTF8.self) } ?? ""
            report.expect(read == code + "\n" && !argv.contains(code) && argv.hasPrefix("stdin\n"),
                          "the code reaches standard input, and is not in argv", "\(String(describing: read)) \(argv)")
            report.expect(ended?.end == .exited(1) && watch.objects == [.error(HostError(code: "held", message: "this computer already holds a pair", kind: nil))],
                          "and the refusal is read")
        }
        // A child that exits without reading the code cannot break the app (no SIGPIPE).
        if let (_, watch) = start(["replay", heldFile, "1"], .code(code)) {
            report.expect(watch.waitForExit(seconds: 10)?.end == .exited(1) && watch.objects.count == 1,
                          "a child that exits before reading the code is read like any other")
        }
        let tooLong = HostChild(cli: fake, verb: ["replay", heldFile, "1"], input: .code(String(repeating: "A", count: 9000)),
                                deliverOn: DispatchQueue(label: "selftest.unused"))
        do {
            try tooLong.start()
            report.expect(false, "a code longer than the pipe takes is refused")
        } catch {
            report.expect((error as? HostChild.StartError) == .inputTooLong && !tooLong.isRunning,
                          "a code longer than the pipe takes is refused, and nothing starts")
        }
        report.expect(fake.run(["replay", heldFile, "1"], input: Data(count: 9000)).status == CLIResult.notStarted,
                      "so is a short run's input")

        // SIGINT reaches a waiting child, which says interrupted.
        let pairCodeFile = file("pair-code.jsonl", pairCodeLine + "\n")
        let interruptedFile = file("interrupted.jsonl", interruptedLine + "\n")
        if let (child, watch) = start(["trap", pairCodeFile, interruptedFile], .code(code)) {
            report.expect(watch.wait(seconds: 10) { !watch.objects.isEmpty }, "the waiting child wrote its code's name")
            child.interrupt()
            let ended = watch.waitForExit(seconds: 10)
            report.expect(ended?.end == .exited(1)
                              && watch.objects.last == .error(HostError(code: "interrupted", message: "m", kind: nil)),
                          "interrupt() sends SIGINT, which a waiting pair answers with interrupted", "\(String(describing: ended))")
        }

        // Lines that cannot be used are dropped, with a note each.
        let hygiene = file(
            "hygiene.jsonl",
            String(repeating: "x", count: 20000) + "\nnot json\n" + stateLine + "\n" + readyLine + "\r\n" + #"{"v":1,"type":"ready""#
        )
        if let (_, watch) = start(["replay", hygiene, "0"]) {
            _ = watch.waitForExit(seconds: 10)
            report.expect(watch.objects.count == 1 && watch.notes.count == 4,
                          "an over-long line, a line that is not JSON, one ending in CR and a last fragment are dropped, each with a note",
                          "\(watch.objects.count) objects, notes \(watch.notes)")
        }

        // Standard error is kept, its last 40 lines.
        let errLines = (1...50).map { "line \($0)" }.joined(separator: "\n") + "\n"
        let errFile = file("stderr.txt", errLines)
        if let (child, watch) = start(["stderr", errFile, "1"]) {
            _ = watch.waitForExit(seconds: 10)
            report.expect(child.stderrTail.count == 40 && child.stderrTail.last == "line 50" && watch.stderr.count == 50,
                          "each line of standard error is delivered, and the last 40 kept", "\(child.stderrTail.count)")
        }

        // A burst: 5000 objects, then the lifeline closes, and nothing waits on anything.
        let burst = file("burst.jsonl", String(repeating: stateLine + "\n", count: 5000))
        if let (child, watch) = start(["lifeline", burst, stoppedFile]) {
            report.expect(watch.wait(seconds: 30) { watch.objects.count >= 5000 }, "5000 objects arrive", "\(watch.objects.count)")
            child.closeLifeline()
            let ended = watch.waitForExit(seconds: 10)
            report.expect(ended?.end == .exited(0) && watch.objects.count == 5001, "and the child then drains and exits",
                          "\(String(describing: ended)) \(watch.objects.count)")
        }
    }

    // MARK: - The real command line: pairing verbs and run

    /// The pairing verbs and `run` against the real host, unpaired, on a throwaway profile. What
    /// `status`, `pair status` and `run` say depends on whether macOS's Keychain can be used under
    /// this HOME, so each takes either answer the host can give, and says which it saw. `pair`
    /// passes `--store-file`, so it reaches the code without the Keychain.
    static func checkPairingVerbs(python: URL, home: String, _ report: Report) {
        let cli = HostCLI(python: python, home: home, profile: "selftest-pair", timeout: 60)
        report.expect(cli.arguments(for: ["status"]) == ["-I", "-B", "-X", "utf8", "-m", "lucille_host", "--profile", "selftest-pair", "status"],
                      "the profile is HostCLI's to choose, for tests")
        let storeCodes: Set<String> = ["insecure_keyring", "store_error"]
        let state = VerbOutcome(cli.run(["--json", "status"]))
        if let found = state.status {
            var known = false
            switch found.pair {
            case .noPair:
                known = true
            case .unreadable(let code):
                known = storeCodes.contains(code)
            default:
                known = false
            }
            report.expect(known && !found.paused, "status --json reads, with no pair or a store it cannot use", "\(found.pair)")
            print("note: status says the pair is \(found.pair) under this HOME")
        } else {
            report.expect(false, "status --json reads", "\(state)")
        }
        let pairState = VerbOutcome(cli.run(["--json", "pair", "status"]))
        switch pairState {
        case .done(let objects):
            report.expect(objects == [.pair(.noPair)], "pair status says there is no pair", "\(objects)")
        case .refused(let error):
            report.expect(storeCodes.contains(error.code), "pair status refuses only for the store", "\(error)")
        default:
            report.expect(false, "pair status answers", "\(pairState)")
        }

        let queue = DispatchQueue(label: "selftest.pair")
        for (given, expected) in [("swbp1_" + String(repeating: "A", count: 120), "bad_code"), ("", "no_code")] {
            let watch = ChildWatch()
            let child = HostChild(cli: cli, verb: ["pair", "--code-stdin", "--store-file", "--json"], input: .code(given), deliverOn: queue)
            watch.watch(child)
            do {
                try child.start()
            } catch {
                report.expect(false, "start pair --code-stdin", "\(error)")
                continue
            }
            let ended = watch.waitForExit(seconds: 60)
            report.expect(ended?.end == .exited(1) && watch.objects.last.map(isError(expected)) == true,
                          "the host reads what was written on standard input: \(expected)",
                          "\(String(describing: ended)) \(watch.objects)")
        }

        let watch = ChildWatch()
        let run = HostChild(cli: cli, verb: ["run", "--json-events", "--exit-on-stdin-eof"], input: .lifeline, deliverOn: queue)
        watch.watch(run)
        do {
            try run.start()
            var ended = watch.waitForExit(seconds: 60)
            if ended == nil {
                run.closeLifeline()
                ended = watch.waitForExit(seconds: 30)
            }
            run.closeLifeline()
            var stopped: StopReason?
            for object in watch.objects {
                if case .stopped(let reason) = object {
                    stopped = reason
                }
            }
            let outcome = ended.map { RunSupervisor.classify(exit: $0.end, stopped: stopped, asked: false) }
            let expected: [RunSupervisor.Outcome] = [.notConnected(.notPaired), .needsAttention(.insecureKeyring),
                                                     .needsAttention(.storeError)]
            report.expect(outcome.map { expected.contains($0) } == true,
                          "run with the lifeline, unpaired, stops at once and is not restarted",
                          "\(String(describing: ended)) \(String(describing: stopped)) \(String(describing: outcome))")
            print("note: run said \(String(describing: stopped)) under this HOME")
        } catch {
            report.expect(false, "start run", "\(error)")
        }
    }

    static func isError(_ code: String) -> (HostObject) -> Bool {
        { object in
            if case .error(let error) = object {
                return error.code == code
            }
            return false
        }
    }
}
