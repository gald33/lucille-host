import AppKit

/// Copy Diagnostics… (decisions/0002 §15): the menu's item reads the versions and `status` again,
/// and a note shows the text `Diagnostics` makes from them, says that leaving things out is best
/// effort, and offers Copy, the default, and Cancel.
///
/// - Only Copy writes the pasteboard, and only that text, as plain text. It is the app's one copy
///   of its own. It goes through a text view's own Copy, as a user's Command-C would, so the
///   app's code never names the pasteboard, and never reads it (tools/guards/macos_lint.py).
/// - Nothing in the note can be selected or edited: the text is copied whole, or not at all.
/// - Nothing is kept: the text is made again each time, and dropped once the note is closed.
@MainActor
enum DiagnosticsPanel {
    private static let boxWidth: CGFloat = 460
    private static let boxHeight: CGFloat = 220

    /// Copy Diagnostics…, chosen in the menu: read the versions and `status`, then show the note.
    /// `status` opens the store that keeps the pair, so it waits its turn with the other verbs
    /// that do.
    static func chosen(_ cli: HostCLI) {
        Task {
            let version = Diagnostics.version(VerbOutcome(await VerbQueue.run(cli, ["--json", "--version"], on: VerbQueue.other)))
            let status = Diagnostics.statusRead(VerbOutcome(await VerbQueue.run(cli, ["--json", "status"], on: VerbQueue.store)))
            let system = ProcessInfo.processInfo.operatingSystemVersion
            let facts = Diagnostics.Facts(
                appVersion: Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String,
                hostVersion: version?.host,
                contract: version?.contract,
                macOS: [system.majorVersion, system.minorVersion, system.patchVersion],
                status: status,
                readAt: Date()
            )
            show(Diagnostics.text(facts))
        }
    }

    private static func show(_ text: String) {
        Alerts.shared.show(key: "diagnostics", {
            let alert = NSAlert()
            alert.messageText = DiagnosticsText.title
            alert.informativeText = DiagnosticsText.explanation
            alert.addButton(withTitle: DiagnosticsText.copyButton)
            alert.addButton(withTitle: DiagnosticsText.cancelButton).keyEquivalent = "\u{1b}"
            alert.accessoryView = box(text)
            return alert
        }, done: { response in
            if response == .alertFirstButtonReturn {
                copy(text)
            }
        })
    }

    /// The text, shown in a box that scrolls, in a fixed-width font, with nothing selectable.
    private static func box(_ text: String) -> NSView {
        let scroll = NSTextView.scrollableTextView()
        scroll.frame = NSRect(x: 0, y: 0, width: boxWidth, height: boxHeight)
        scroll.hasVerticalScroller = true
        if let view = scroll.documentView as? NSTextView {
            view.isRichText = false
            view.string = text
            view.isEditable = false
            view.isSelectable = false
            view.font = .monospacedSystemFont(ofSize: 11, weight: .regular)
        }
        return scroll
    }

    /// Copy's click: the whole text, as plain text, through a text view of its own that no window
    /// shows.
    private static func copy(_ text: String) {
        let view = NSTextView(frame: NSRect(x: 0, y: 0, width: boxWidth, height: boxHeight))
        view.isRichText = false
        view.string = text
        view.isEditable = false
        view.selectAll(nil)
        view.copy(nil)
    }
}
