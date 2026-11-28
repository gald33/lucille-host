import Foundation

/// What Copy Diagnostics… copies (decisions/0002 §15), as text, from what the app reads again when
/// it is chosen: the app's version, `--json --version`, the macOS version, and `status --json`
/// with folders by alias only. It draws nothing; `DiagnosticsPanel` shows the text, and only its
/// Copy button puts it on the pasteboard.
///
/// The host has no `diagnostics` verb yet (spec/CLI.md §14), which is where §15 puts the redaction
/// and the last 20 audit lines. Until it has, the app writes no audit line and no text the host
/// wrote for people (a `message`, an error's words): only fields of a closed shape, each checked
/// against that shape here, and a value that fails its check is written as `unknown`. So it never
/// writes a path, a body, a key, a code, a pair's name, a room's name or a process id.
enum Diagnostics {
    /// How the `status` read ended.
    enum StatusRead: Equatable, Sendable {
        case read(HostStatus)
        /// The host refused it, with this `code`.
        case refused(code: String)
        /// It did not end in an answer: it timed out, was stopped, or could not start.
        case failed
    }

    /// What one Copy Diagnostics… read.
    struct Facts: Equatable, Sendable {
        /// `CFBundleShortVersionString` of the app.
        var appVersion: String?
        /// `host_version` and `contract` of `--json --version`.
        var hostVersion: String?
        var contract: String?
        /// The macOS version, major, minor and patch.
        var macOS: [Int]
        var status: StatusRead
        /// When the app read them, by its own clock.
        var readAt: Date
    }

    /// The closed values `status` gives for why the pair can't be read and why this Mac last left
    /// its room (spec/CLI.md §6): another value is written as `unknown`.
    static let unreadableCodes: Set<String> = ["insecure_keyring", "store_error", "pair_unusable"]
    static let leftBecause: Set<String> = ["check_3", "check_4", "left_before"]

    /// `--json --version`'s release and contract, when it worked.
    static func version(_ outcome: VerbOutcome) -> (host: String, contract: String)? {
        guard case .done(let objects) = outcome else {
            return nil
        }
        for object in objects {
            if case .version(let host, let contract) = object {
                return (host, contract)
            }
        }
        return nil
    }

    /// How `status --json` ended. A refusal keeps its `code` only: its message is the host's
    /// words, which may name a path.
    static func statusRead(_ outcome: VerbOutcome) -> StatusRead {
        if let status = outcome.status {
            return .read(status)
        }
        if case .refused(let error) = outcome {
            return .refused(code: error.code)
        }
        return .failed
    }

    /// The text Copy copies: one `name: value` line each, named as in `status --json`.
    static func text(_ facts: Facts) -> String {
        var lines = [
            "\(DiagnosticsText.heading), \(time(facts.readAt))",
            "app_version: \(shown(facts.appVersion, if: isVersion))",
            "host_version: \(shown(facts.hostVersion, if: isVersion))",
            "contract: \(shown(facts.contract, if: isContract))",
            "macos_version: \(macOS(facts.macOS))",
        ]
        switch facts.status {
        case .read(let status):
            lines.append("status:")
            lines += statusLines(status).map { "  " + $0 }
        case .refused(let code):
            lines.append("status: refused, \(shown(code, if: isCode))")
        case .failed:
            lines.append("status: no answer")
        }
        lines.append("audit: \(DiagnosticsText.noAudit)")
        return lines.joined(separator: "\n") + "\n"
    }

    private static func statusLines(_ status: HostStatus) -> [String] {
        var lines = [
            "host_version: \(shown(status.hostVersion, if: isVersion))",
            "paused: \(status.paused)",
            "policy: \(status.policyOK ? "ok" : "invalid")",
        ]
        if status.policyOK {
            lines.append("folders: \(list(status.folders, if: Alias.isValid))")
            lines.append("enabled: \(list(status.enabledOps, if: isOpName))")
        }
        lines.append("pair: \(pair(status.pair))")
        lines.append("run: \(run(status.run))")
        lines.append("last_hub_contact: \(status.lastHubContact.map(time) ?? "none")")
        lines.append("last_left_because: \(status.lastLeftBecause.map { leftBecause.contains($0) ? $0 : "unknown" } ?? "none")")
        lines.append("clock: \(clock(status.clock))")
        return lines
    }

    /// The pair's state alone: never its name or deadline.
    private static func pair(_ pair: StatusPair) -> String {
        switch pair {
        case .noPair:
            return "none"
        case .waiting:
            return "waiting"
        case .sasReady:
            return "sas_ready"
        case .sasPending:
            return "sas_pending"
        case .confirmed:
            return "confirmed"
        case .refused:
            return "refused"
        case .expired:
            return "expired"
        case .severalPairs:
            return "several_pairs"
        case .unreadable(let code):
            return "unreadable, " + (unreadableCodes.contains(code) ? code : "unknown")
        case .other:
            return "unknown"
        }
    }

    /// The state of `run`'s record alone: never its process id.
    private static func run(_ run: StatusRun) -> String {
        switch run {
        case .noRecord:
            return "none"
        case .running:
            return "running"
        case .notSeen:
            return "not_seen"
        case .stopped:
            return "stopped"
        case .other:
            return "unknown"
        }
    }

    private static func clock(_ clock: ClockState) -> String {
        switch clock {
        case .ok(let skew):
            return "ok, skew_s \(seconds(skew))"
        case .skewed(let skew):
            return "skewed, skew_s \(skew.map(seconds) ?? "unknown")"
        case .unknown:
            return "unknown"
        }
    }

    private static func seconds(_ value: Double) -> String {
        guard value.isFinite, abs(value) < 1e9 else {
            return "unknown"
        }
        return String(format: "%.1f", value)
    }

    private static func macOS(_ parts: [Int]) -> String {
        guard parts.count == 3, parts.allSatisfy({ (0...999).contains($0) }) else {
            return "unknown"
        }
        return parts.map(String.init).joined(separator: ".")
    }

    private static func shown(_ value: String?, if valid: (String) -> Bool) -> String {
        guard let value, valid(value) else {
            return "unknown"
        }
        return value
    }

    /// The valid values, in order, joined; one that is not valid is counted, never written.
    private static func list(_ values: [String], if valid: (String) -> Bool) -> String {
        let good = values.filter(valid)
        let bad = values.count - good.count
        var parts = good
        if bad > 0 {
            parts.append("\(bad) unknown")
        }
        return parts.isEmpty ? "none" : parts.joined(separator: ", ")
    }

    private static func time(_ date: Date) -> String {
        let formatter = ISO8601DateFormatter()
        formatter.timeZone = TimeZone(identifier: "UTC")
        formatter.formatOptions = [.withInternetDateTime]
        return formatter.string(from: date)
    }

    private static func ascii(_ value: String, max: Int, _ allowed: (Unicode.Scalar) -> Bool) -> Bool {
        let scalars = Array(value.unicodeScalars)
        return !scalars.isEmpty && scalars.count <= max && scalars.allSatisfy(allowed)
    }

    private static func isLower(_ s: Unicode.Scalar) -> Bool { s.value >= 0x61 && s.value <= 0x7A }
    private static func isUpper(_ s: Unicode.Scalar) -> Bool { s.value >= 0x41 && s.value <= 0x5A }
    private static func isDigit(_ s: Unicode.Scalar) -> Bool { s.value >= 0x30 && s.value <= 0x39 }

    /// A release's version, as the contract's `host_version`: `[0-9][0-9A-Za-z.+_-]{0,31}`.
    static func isVersion(_ value: String) -> Bool {
        guard let first = value.unicodeScalars.first, isDigit(first) else {
            return false
        }
        return ascii(value, max: 32) { isLower($0) || isUpper($0) || isDigit($0) || ".+_-".unicodeScalars.contains($0) }
    }

    /// The contract's version: `1.<minor>`.
    static func isContract(_ value: String) -> Bool {
        guard value.hasPrefix("1."), value.count <= 6 else {
            return false
        }
        let minor = value.dropFirst(2)
        return ascii(String(minor), max: 4, isDigit) && (minor == "0" || !minor.hasPrefix("0"))
    }

    /// An operation's name: `[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+`, at most 64 characters.
    static func isOpName(_ value: String) -> Bool {
        guard ascii(value, max: 64, { isLower($0) || isDigit($0) || $0 == "_" || $0 == "." }) else {
            return false
        }
        let parts = value.split(separator: ".", omittingEmptySubsequences: false)
        return parts.count >= 2 && parts.allSatisfy { part in
            part.unicodeScalars.first.map(isLower) ?? false
        }
    }

    /// A refusal's code: `[a-z][a-z0-9_]*`, at most 64 characters.
    static func isCode(_ value: String) -> Bool {
        guard let first = value.unicodeScalars.first, isLower(first) else {
            return false
        }
        return ascii(value, max: 64) { isLower($0) || isDigit($0) || $0 == "_" }
    }
}

/// Copy Diagnostics…'s words.
enum DiagnosticsText {
    static let menuItem = "Copy Diagnostics…"
    static let title = "Copy Diagnostics"
    static let heading = "\(HostText.appName) diagnostics"
    static let explanation =
        "This is what \(HostText.appName) copies when you click Copy, and nothing goes anywhere until you paste it. It leaves out paths, file contents, keys, codes and the names of connections and rooms, as best it can: read it before you send it to anyone."
    static let copyButton = "Copy"
    static let cancelButton = "Cancel"
    static let noAudit = "left out; this host has no diagnostics command yet"
}
