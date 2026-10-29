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
            let got = Alias.suggest(forFolderName: vector.folder)
            report.expect(got == vector.alias, "suggest(\(vector.folder.debugDescription)) is \(vector.alias)", got)
            report.expect(Alias.isValid(got), "\(got) is a valid alias")
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
        report.expect(HostText.failureMessage(usage) == "The following arguments are required: path.",
                      "a usage error reads as one sentence", HostText.failureMessage(usage))
        let refused = CLIResult(status: 1, stdout: "",
                                stderr: "lucille-host: no folder alias 'x'\n", timedOut: false)
        report.expect(HostText.failureMessage(refused) == "No folder alias 'x'.",
                      "a refusal reads without the program name", HostText.failureMessage(refused))
        let late = CLIResult(status: 15, stdout: "", stderr: "", timedOut: true)
        report.expect(!HostText.failureMessage(late).isEmpty && !HostText.needsHomeConfirmation(late),
                      "a timeout has its own words")
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
        report.expect(!missing.succeeded && HostText.failureMessage(missing).hasPrefix("No folder alias"),
                      "removing it twice is refused in plain words", HostText.failureMessage(missing))

        let broad = cli.run(["roots", "add", "home", home])
        report.expect(HostText.needsHomeConfirmation(broad), "the home folder needs confirmation", "\(broad)")
        let confirmed = cli.run(["roots", "add", "home", home, "--allow-home-or-root"])
        report.expect(confirmed.succeeded, "and is added once confirmed", "\(confirmed)")
        report.expect(cli.run(["roots", "remove", "home"]).succeeded, "and removed again")

        let slow = HostCLI(python: cli.python, home: home, timeout: 0.001).run(["ops", "list"])
        report.expect(slow.timedOut && !slow.succeeded, "a run past its time limit is stopped", "\(slow)")
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
