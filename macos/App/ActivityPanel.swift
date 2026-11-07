import AppKit

/// The Activity window (decisions/0002 §15): what Lucille asked this Mac to do since the app
/// opened, newest first, each request with the time the app first saw it, in the user's format,
/// and the words `RequestText` gives it; then, behind a disclosure, the host's last 200 log lines,
/// as the host wrote them. It shows what the app gives it and says when its disclosure opens; it
/// decides nothing, and keeps nothing once the app quits.
///
/// - Nothing in it can be selected or edited, so nothing in it can be taken out of it: that would
///   write the pasteboard, and the app adds no copy of its own (Copy Diagnostics, §15, is later). It
///   has no button on a request: a refusal is a fact, never an offer to turn an ability on (§13).
/// - It is one non-modal panel that stays on the screen while the user works in another app. It is
///   not restorable and keeps no frame of its own: macOS would otherwise keep the window's state
///   in a folder named after the app, and a frame is a setting.
/// - Its words and its one button sit straight in the window, as in a note; the requests and the
///   log each scroll in a box of their own.
@MainActor
final class ActivityPanel: NSObject {
    /// The disclosure opened: the app reads the host's log, and gives it to `showLog`.
    var onShowLog: (() -> Void)?

    private let width: CGFloat = 520
    private let panel: NSPanel
    private let content: NSView
    private let header: NSTextField
    private let requestsBox: NSScrollView
    private let toggle: NSButton
    private let caption: NSTextField
    private let logBox: NSScrollView
    /// The window's last view, and so its height: the disclosure's button while the log is hidden,
    /// the log's box while it shows.
    private let closedBottom: NSLayoutConstraint
    private let openBottom: NSLayoutConstraint
    private var entries: [RequestEntry] = []
    private var summaries: [String: String] = [:]
    private var logOpen = false

    override init() {
        panel = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: 520, height: 400),
            styleMask: [.titled, .closable],
            backing: .buffered,
            defer: true
        )
        content = NSView()
        header = NSTextField(wrappingLabelWithString: ConnectionText.activityHeader)
        requestsBox = NSTextView.scrollableTextView()
        toggle = NSButton(title: ConnectionText.showLog, target: nil, action: nil)
        caption = NSTextField(wrappingLabelWithString: ConnectionText.logCaption)
        logBox = NSTextView.scrollableTextView()
        closedBottom = toggle.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -20)
        openBottom = logBox.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -20)
        super.init()
        panel.title = ConnectionText.activityTitle
        panel.isReleasedWhenClosed = false
        panel.hidesOnDeactivate = false
        panel.isRestorable = false

        for label in [header, caption] {
            label.isSelectable = false
            label.preferredMaxLayoutWidth = width - 40
        }
        caption.textColor = .secondaryLabelColor
        caption.isHidden = true
        for box in [requestsBox, logBox] {
            box.hasVerticalScroller = true
            box.borderType = .bezelBorder
            if let text = box.documentView as? NSTextView {
                text.isEditable = false
                text.isSelectable = false
                text.textContainerInset = NSSize(width: 4, height: 6)
            }
        }
        logBox.isHidden = true
        toggle.target = self
        toggle.action = #selector(toggled(_:))

        let views: [NSView] = [header, requestsBox, toggle, caption, logBox]
        for view in views {
            view.translatesAutoresizingMaskIntoConstraints = false
            content.addSubview(view)
        }
        NSLayoutConstraint.activate([
            content.widthAnchor.constraint(equalToConstant: width),
            header.topAnchor.constraint(equalTo: content.topAnchor, constant: 20),
            header.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 20),
            header.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -20),
            requestsBox.topAnchor.constraint(equalTo: header.bottomAnchor, constant: 12),
            requestsBox.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 20),
            requestsBox.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -20),
            requestsBox.heightAnchor.constraint(equalToConstant: 240),
            toggle.topAnchor.constraint(equalTo: requestsBox.bottomAnchor, constant: 12),
            toggle.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 20),
            caption.topAnchor.constraint(equalTo: toggle.bottomAnchor, constant: 8),
            caption.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 20),
            caption.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -20),
            logBox.topAnchor.constraint(equalTo: caption.bottomAnchor, constant: 8),
            logBox.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 20),
            logBox.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -20),
            logBox.heightAnchor.constraint(equalToConstant: 200),
            closedBottom,
        ])
        panel.contentView = content
        fit()
    }

    /// Show the window, at the front, with these requests.
    func show(_ shown: [RequestEntry]) {
        entries = shown
        drawRequests()
        if !panel.isVisible {
            panel.center()
        }
        NSApp.activate()
        panel.makeKeyAndOrderFront(nil)
        panel.orderFrontRegardless()
    }

    /// The requests as they are now, shown at once while the window is open.
    func update(_ shown: [RequestEntry]) {
        entries = shown
        if panel.isVisible {
            drawRequests()
        }
    }

    /// The core's summary of each operation, by name, which each request's words start from.
    func setSummaries(_ found: [String: String]) {
        summaries = found
        if panel.isVisible {
            drawRequests()
        }
    }

    /// The host's log, oldest first, each line as the host wrote it, cut for the screen.
    func showLog(_ lines: [String]) {
        setLog(lines.isEmpty ? ConnectionText.logEmpty : lines.map(RequestText.logLine).joined(separator: "\n"))
    }

    func showLogFailed() {
        setLog(ConnectionText.couldNotReadLog)
    }

    // MARK: Drawing

    private func drawRequests() {
        guard let text = requestsBox.documentView as? NSTextView else {
            return
        }
        let spacing = NSMutableParagraphStyle()
        spacing.paragraphSpacing = 8
        let words: [NSAttributedString.Key: Any] = [
            .font: NSFont.systemFont(ofSize: NSFont.systemFontSize),
            .foregroundColor: NSColor.labelColor,
            .paragraphStyle: spacing,
        ]
        let when: [NSAttributedString.Key: Any] = [
            .font: NSFont.systemFont(ofSize: NSFont.smallSystemFontSize),
            .foregroundColor: NSColor.secondaryLabelColor,
            .paragraphStyle: spacing,
        ]
        let today = DateFormatter()
        today.dateStyle = .none
        today.timeStyle = .short
        let earlier = DateFormatter()
        earlier.dateStyle = .short
        earlier.timeStyle = .short
        let shown = NSMutableAttributedString()
        if entries.isEmpty {
            shown.append(NSAttributedString(string: ConnectionText.activityEmpty, attributes: words))
        }
        for (index, entry) in entries.reversed().enumerated() {
            if index > 0 {
                shown.append(NSAttributedString(string: "\n", attributes: words))
            }
            let clock = Calendar.current.isDateInToday(entry.firstSeen) ? today : earlier
            shown.append(NSAttributedString(string: clock.string(from: entry.firstSeen) + "  ", attributes: when))
            var said = RequestText.line(for: entry, summaries: summaries)
            if let outcome = RequestText.outcome(for: entry) {
                said += " " + outcome
            }
            shown.append(NSAttributedString(string: said, attributes: words))
        }
        text.textStorage?.setAttributedString(shown)
    }

    private func setLog(_ words: String) {
        guard let text = logBox.documentView as? NSTextView else {
            return
        }
        text.string = words
        text.font = .monospacedSystemFont(ofSize: 11, weight: .regular)
        text.textColor = .labelColor
    }

    /// Size the window to what it holds, keeping its top left corner where it is.
    private func fit() {
        content.layoutSubtreeIfNeeded()
        let height = content.fittingSize.height
        let top = panel.frame.maxY
        let left = panel.frame.minX
        panel.setContentSize(NSSize(width: width, height: height))
        if panel.isVisible {
            panel.setFrameTopLeftPoint(NSPoint(x: left, y: top))
        }
    }

    /// Show the Host's Log, or Hide the Host's Log. Each time it opens, the log is read again.
    @objc private func toggled(_ sender: Any?) {
        logOpen.toggle()
        toggle.title = logOpen ? ConnectionText.hideLog : ConnectionText.showLog
        caption.isHidden = !logOpen
        logBox.isHidden = !logOpen
        if logOpen {
            closedBottom.isActive = false
            openBottom.isActive = true
            setLog(ConnectionText.readingLog)
        } else {
            openBottom.isActive = false
            closedBottom.isActive = true
        }
        fit()
        if logOpen {
            onShowLog?()
        }
    }
}
