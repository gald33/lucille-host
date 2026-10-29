import Foundation

/// Runs the app's code that draws nothing (`Alias`, `HostCLI`, `HostText`) against a built app:
/// the calls the menu makes, through the same code, with a temporary HOME. CI compiles it with
/// those files (`macos/check_app.py swift`). Exit status 1 on any failure.
///
///     selftest --vectors alias-vectors.json [--app "Lucille Host.app" --home DIR --version V]
@main
struct SelfTest {
    static func main() {
        var options: [String: String] = [:]
        var args = CommandLine.arguments.dropFirst()
        while let key = args.popFirst() {
            guard key.hasPrefix("--"), let value = args.popFirst() else {
                print("usage: selftest --vectors FILE [--app APP --home DIR --version V]")
                exit(2)
            }
            options[String(key.dropFirst(2))] = value
        }
        let report = Report()
        if let vectors = options["vectors"] {
            checkAliases(vectorsAt: vectors, report)
        }
        checkText(report)
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
                          message: "Something went wrong in Lucille Host's helper program.",
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
             "Lucille Host can use at most 64 folders. Remove one before you add another."),
            ("/Users/a/Library/Application Support/lucille-host is where lucille-host keeps its own files",
             "That folder holds Lucille Host's own settings, so it can't be added."),
            ("the policy file /x/policy.json is invalid: ops.files.sha256.definition must be 64 lowercase hex digits",
             "Lucille Host's settings file can't be read."),
            ("cannot lock /x/.policy.lock (Operation not supported), so the policy was not changed",
             "Lucille Host couldn't lock its settings file, so nothing was changed. Try again."),
            ("files.sha256 is not enabled", "That ability was already off."),
            ("/gone is not a folder", "That folder is no longer there."),
            ("something new", "Something went wrong in Lucille Host's helper program."),
        ]
        for (text, message) in plain {
            let got = HostText.explain(refusal(text))
            report.expect(got.message == message && got.detail == text,
                          "the refusal \(text.debugDescription) reads as \(message.debugDescription)", "\(got)")
        }
        let late = CLIResult(status: 15, stdout: "", stderr: "", timedOut: true)
        report.expect(HostText.explain(late, during: .addingFolder).message.hasPrefix("Checking this folder took too long")
                          && HostText.explain(late).message != HostText.explain(late, during: .addingFolder).message
                          && !HostText.needsHomeConfirmation(late),
                      "a timeout has its own words, and says more while adding a folder")
        let broken = CLIResult(status: CLIResult.notStarted, stdout: "", stderr: "no such file", timedOut: false)
        report.expect(HostText.explain(broken) == Explanation(message: "Lucille Host is damaged. Download it again.",
                                                              detail: "no such file"),
                      "a helper program that cannot start says to download the app again")
        let silent = CLIResult(status: 3, stdout: "", stderr: "", timedOut: false)
        report.expect(HostText.explain(silent).message == "Something went wrong in Lucille Host's helper program."
                          && HostText.explain(silent).detail == "It stopped with exit status 3.",
                      "a failure with no reason names no status in its sentence")

        report.expect(HostText.broadFolder(path: "/Users/a", home: "/Users/a") == .home, "the home folder is broad")
        report.expect(HostText.broadFolder(path: "/Users/a/", home: "/Users/a") == .home, "with a slash too")
        report.expect(HostText.broadFolder(path: "/Users", home: "/Users/a") == .holdsHome, "a folder holding it is")
        report.expect(HostText.broadFolder(path: "/", home: "/Users/a") == .disk, "the top of the disk is")
        report.expect(HostText.broadFolder(path: "/Users/a/Documents", home: "/Users/a") == nil, "a folder inside is not")
        report.expect(HostText.broadFolder(path: "/Users/ab", home: "/Users/a") == nil, "a sibling is not")
        let disk = HostText.broadFolderQuestion(.disk, diskName: "Macintosh HD")
        report.expect(disk.text.hasPrefix("Macintosh HD is your whole startup disk"), "the disk is named as Finder names it")
        for kind in [BroadFolder.home, .holdsHome, .disk] {
            let question = HostText.broadFolderQuestion(kind, diskName: "Macintosh HD")
            report.expect(question.text.contains("Documents, Desktop and Downloads") && question.text.contains("Library"),
                          "the question for \(kind) says what is inside")
        }
        let prompt = HostText.aliasPrompt(folderName: "Taxes", note: "Note.")
        report.expect(prompt.hasPrefix("Note.\n\nLucille will know") && prompt.contains("never learns where"),
                      "the name dialog leads with its note, then its purpose", prompt)
    }

    static func checkCLI(app: URL, home: String, version: String, _ report: Report) {
        let cli = HostCLI(python: HostCLI.python(inApp: app), home: home)
        let env = cli.environment()
        report.expect(Set(env.keys).isSubset(of: ["HOME", "PATH", "LANG", "USER", "LOGNAME", "TMPDIR"]),
                      "the environment is fixed", "\(env.keys.sorted())")
        report.expect(cli.arguments(for: ["pause"]) == ["-I", "-B", "-m", "lucille_host", "pause"],
                      "the interpreter runs isolated, without writing bytecode")

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

        let paused = cli.run(["pause"])
        report.expect(paused.succeeded && cli.isPaused, "pause shows as paused", "\(paused)")
        let resumed = cli.run(["resume"])
        report.expect(resumed.succeeded && !cli.isPaused, "resume shows as not paused", "\(resumed)")

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
                          && HostText.explain(ownFolder).message == "That folder holds Lucille Host's own settings, so it can't be added.",
                      "the host's own folder is refused in plain words", "\(HostText.explain(ownFolder))")
        let off = cli.run(["ops", "disable", "files.sha256"])
        report.expect(!off.succeeded && HostText.explain(off).message == "That ability was already off.",
                      "turning off an ability that is off is refused in plain words", "\(HostText.explain(off))")

        let broad = cli.run(["roots", "add", "home", home])
        report.expect(HostText.needsHomeConfirmation(broad), "the home folder needs confirmation", "\(broad)")
        let confirmed = cli.run(["roots", "add", "home", home, "--allow-home-or-root"])
        report.expect(confirmed.succeeded, "and is added once confirmed", "\(confirmed)")
        report.expect(cli.run(["roots", "remove", "home"]).succeeded, "and removed again")

        let slow = HostCLI(python: cli.python, home: home, timeout: 0.001).run(["ops", "list"])
        report.expect(slow.timedOut && !slow.succeeded, "a run past its time limit is stopped", "\(slow)")
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
