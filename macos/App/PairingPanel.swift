import AppKit

/// The window the app pairs in, on decisions/0001's gate: a page for the code, one while it waits
/// for Lucille, one to compare the numbers, and one while a verb it asked for runs. It shows what
/// `PairingFlow`'s screen says and tells the app what the user did; it decides nothing itself.
///
/// - It is one non-modal panel that stays on the screen while the user looks at Lucille's page in
///   another app, above other windows.
/// - The user pastes the code into its field: the app never reads the pasteboard. Command-V, C, X,
///   A and Z reach the field through the panel, as an Edit menu would send them.
/// - The number counts as drawn only once it is on the screen: the panel visible and not wholly
///   hidden by other windows (macOS's occlusion state says only that some part of it shows), a
///   moment after it was set. They Match and They Differ stay off until then, neither is a
///   default button, and the compare page starts with neither focused.
/// - A click on They Match carries its event's time, so the flow can refuse a click made before
///   the number was drawn.
/// - Its views sit straight in the window, in no group, as in a note.
@MainActor
final class PairingPanel: NSObject {
    /// What the user did. The app's glue turns each into an input of `PairingFlow`.
    var onSubmit: ((String) -> Void)?
    /// The window's close button, or Cancel on the code page.
    var onClose: (() -> Void)?
    /// Cancel on the waiting page: the app asks whether to stop connecting.
    var onStopAsked: (() -> Void)?
    /// Cancel while a verb runs.
    var onCancelBusy: (() -> Void)?
    /// The number is on the screen, at this time (`ProcessInfo.systemUptime`).
    var onDrawn: ((String, TimeInterval) -> Void)?
    /// They Match, at the time of the event that pressed it, on the same clock.
    var onMatch: ((TimeInterval) -> Void)?
    var onDiffer: (() -> Void)?

    private enum Page: Equatable {
        case none
        case code
        case waiting(clockSkewed: Bool)
        case compare
        case busy(PairingFlow.Busy)
    }

    private let width: CGFloat = 460
    private let panel: GatePanel
    private var page: Page = .none
    private var content: NSView
    private var field: CodeField?
    private var hint: NSTextField?
    private var connect: NSButton?
    private var match: NSButton?
    private var differ: NSButton?
    /// The number the compare page shows, and whether the flow has been told it is drawn.
    private var shown: PairingFlow.Comparison?
    private var reported = false
    private let drawCheck: MainTimer
    private var occlusionObserver: NSObjectProtocol?

    override init() {
        panel = GatePanel(
            contentRect: NSRect(x: 0, y: 0, width: 460, height: 200),
            styleMask: [.titled, .closable],
            backing: .buffered,
            defer: true
        )
        content = NSView()
        drawCheck = MainTimer()
        super.init()
        panel.isReleasedWhenClosed = false
        panel.level = .floating
        panel.hidesOnDeactivate = false
        panel.title = ConnectionText.codeTitle
        panel.onCloseAsked = { [weak self] in
            self?.onClose?()
        }
        occlusionObserver = NotificationCenter.default.addObserver(
            forName: NSWindow.didChangeOcclusionStateNotification, object: panel, queue: .main
        ) { [weak self] _ in
            onMain {
                self?.checkDrawn()
            }
        }
    }

    var isVisible: Bool { panel.isVisible }

    /// The menu chose the window while it is open: bring it to the front.
    func bringForward() {
        guard page != .none else {
            return
        }
        NSApp.activate()
        panel.makeKeyAndOrderFront(nil)
        panel.orderFrontRegardless()
    }

    func hide() {
        guard page != .none else {
            return
        }
        clearCode()
        drawCheck.cancel()
        page = .none
        shown = nil
        reported = false
        panel.orderOut(nil)
    }

    /// The code page. `setAside` adds the sentence that connecting sets the folders and abilities
    /// aside.
    func showCode(setAside: Bool) {
        let fresh = page != .code
        if fresh {
            install(codePage(setAside: setAside), title: ConnectionText.codeTitle, page: .code)
            panel.initialFirstResponder = field
        }
        present()
        if fresh, let field {
            _ = panel.makeFirstResponder(field)
        }
    }

    func showWaiting(clockSkewed: Bool) {
        if page != .waiting(clockSkewed: clockSkewed) {
            install(waitingPage(clockSkewed: clockSkewed), title: ConnectionText.codeTitle,
                    page: .waiting(clockSkewed: clockSkewed))
        }
        present()
    }

    /// The compare page. A new number, or one the flow wants drawn again, is reported drawn once
    /// it is on the screen; until the flow has that, both buttons are off.
    func showCompare(_ comparison: PairingFlow.Comparison) {
        let same = page == .compare && shown.map {
            $0.name == comparison.name && $0.sas == comparison.sas
                && $0.deadline == comparison.deadline && $0.canMatch == comparison.canMatch
        } == true
        if !same {
            shown = comparison
            reported = false
            install(comparePage(comparison), title: ConnectionText.compareTitle, page: .compare)
            panel.initialFirstResponder = nil
            _ = panel.makeFirstResponder(nil)
        }
        let drawn = comparison.drawnAt != nil
        if !drawn && reported {
            // The flow asks for the number to be drawn again: it read a new `sas_pending`.
            reported = false
        }
        match?.isEnabled = drawn && comparison.canMatch
        differ?.isEnabled = drawn
        present()
        if !drawn && !reported {
            panel.displayIfNeeded()
            drawCheck.schedule(after: 0.05) { [weak self] in
                self?.checkDrawn()
            }
        }
    }

    func showBusy(_ busy: PairingFlow.Busy) {
        if page != .busy(busy) {
            let title = page == .compare ? ConnectionText.compareTitle : ConnectionText.codeTitle
            install(busyPage(busy), title: title, page: .busy(busy))
        }
        present()
    }

    /// Empty the code field, and what the window's field editor and its undo still hold of it.
    func clearCode() {
        guard let field else {
            return
        }
        panel.endEditing(for: nil)
        field.stringValue = ""
        panel.fieldEditor(false, for: nil)?.string = ""
        panel.undoManager?.removeAllActions()
        codeChanged("")
    }

    // MARK: Pages

    private func codePage(setAside: Bool) -> NSView {
        let field = CodeField(string: "")
        field.placeholderString = ConnectionText.codePlaceholder
        field.setAccessibilityLabel(ConnectionText.codePlaceholder)
        field.usesSingleLineMode = true
        field.cell?.wraps = false
        field.cell?.isScrollable = true
        field.onChange = { [weak self] text in
            self?.codeChanged(text)
        }
        let hint = label("", secondary: true)
        let connect = NSButton(title: ConnectionText.connectButton, target: self, action: #selector(connectPressed(_:)))
        connect.keyEquivalent = "\r"
        connect.isEnabled = false
        let cancel = NSButton(title: ConnectionText.cancelButton, target: self, action: #selector(closePressed(_:)))
        cancel.keyEquivalent = "\u{1b}"
        var rows: [NSView] = [
            label(ConnectionText.codeSteps),
            label(ConnectionText.codePasteHow),
            label(ConnectionText.codeWarning),
        ]
        if setAside {
            rows.append(label(ConnectionText.settingsSetAside))
        }
        rows += [field, hint]
        self.field = field
        self.hint = hint
        self.connect = connect
        return layout(rows, buttons: [cancel, connect])
    }

    private func waitingPage(clockSkewed: Bool) -> NSView {
        let title = label(ConnectionText.waitingTitle)
        title.font = .boldSystemFont(ofSize: NSFont.systemFontSize)
        var rows: [NSView] = [title, label(ConnectionText.waitingKeepOpen)]
        if clockSkewed {
            rows.append(label(ConnectionText.waitingClockWrong))
        }
        let cancel = NSButton(title: ConnectionText.cancelButton, target: self, action: #selector(stopPressed(_:)))
        cancel.keyEquivalent = "\u{1b}"
        return layout(rows, buttons: [cancel])
    }

    private func comparePage(_ comparison: PairingFlow.Comparison) -> NSView {
        let question = label(ConnectionText.compareQuestion)
        question.font = .boldSystemFont(ofSize: NSFont.systemFontSize)
        let number = NSTextField(labelWithString: comparison.sas)
        number.font = .monospacedDigitSystemFont(ofSize: 34, weight: .semibold)
        number.alignment = .center
        let rule = label(comparison.canMatch ? ConnectionText.compareRule : ConnectionText.compareDiffered)
        let deadline = label(ConnectionText.answerBy(comparison.deadline), secondary: true)
        // Neither button has a key equivalent: Return and Escape answer nothing here.
        let differ = NSButton(title: ConnectionText.theyDiffer, target: self, action: #selector(differPressed(_:)))
        let match = NSButton(title: ConnectionText.theyMatch, target: self, action: #selector(matchPressed(_:)))
        differ.isEnabled = false
        match.isEnabled = false
        self.differ = differ
        self.match = match
        return layout([question, number, rule, deadline], buttons: [differ, match])
    }

    private func busyPage(_ busy: PairingFlow.Busy) -> NSView {
        let words: String
        switch busy {
        case .checking:
            words = ConnectionText.checking
        case .confirming:
            words = ConnectionText.confirming
        case .rejecting:
            words = ConnectionText.turningDown
        }
        let row = NSView()
        let spinner = NSProgressIndicator()
        spinner.style = .spinning
        spinner.controlSize = .small
        spinner.isIndeterminate = true
        spinner.startAnimation(nil)
        let text = NSTextField(labelWithString: words)
        for view in [spinner, text] {
            view.translatesAutoresizingMaskIntoConstraints = false
            row.addSubview(view)
        }
        NSLayoutConstraint.activate([
            spinner.leadingAnchor.constraint(equalTo: row.leadingAnchor),
            spinner.centerYAnchor.constraint(equalTo: text.centerYAnchor),
            text.leadingAnchor.constraint(equalTo: spinner.trailingAnchor, constant: 10),
            text.trailingAnchor.constraint(lessThanOrEqualTo: row.trailingAnchor),
            text.topAnchor.constraint(equalTo: row.topAnchor),
            text.bottomAnchor.constraint(equalTo: row.bottomAnchor),
        ])
        let cancel = NSButton(title: ConnectionText.cancelButton, target: self, action: #selector(cancelBusyPressed(_:)))
        cancel.keyEquivalent = "\u{1b}"
        return layout([row], buttons: [cancel])
    }

    private func label(_ text: String, secondary: Bool = false) -> NSTextField {
        let made = NSTextField(wrappingLabelWithString: text)
        made.preferredMaxLayoutWidth = width - 40
        if secondary {
            made.textColor = .secondaryLabelColor
        }
        return made
    }

    /// `rows` one under the other, full width, then `buttons` in one row at the right, the last
    /// one rightmost.
    private func layout(_ rows: [NSView], buttons: [NSButton]) -> NSView {
        let view = NSView()
        for child in rows + buttons {
            child.translatesAutoresizingMaskIntoConstraints = false
            view.addSubview(child)
        }
        var constraints = [view.widthAnchor.constraint(equalToConstant: width)]
        var above = view.topAnchor
        var gap: CGFloat = 20
        for row in rows {
            constraints += [
                row.topAnchor.constraint(equalTo: above, constant: gap),
                row.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: 20),
                row.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -20),
            ]
            above = row.bottomAnchor
            gap = 12
        }
        var right = view.trailingAnchor
        var inset: CGFloat = -20
        for button in buttons.reversed() {
            constraints += [
                button.topAnchor.constraint(equalTo: above, constant: 20),
                button.trailingAnchor.constraint(equalTo: right, constant: inset),
                button.bottomAnchor.constraint(equalTo: view.bottomAnchor, constant: -20),
            ]
            right = button.leadingAnchor
            inset = -12
        }
        if buttons.isEmpty {
            constraints.append(above.constraint(equalTo: view.bottomAnchor, constant: -20))
        }
        NSLayoutConstraint.activate(constraints)
        return view
    }

    private func install(_ view: NSView, title: String, page newPage: Page) {
        if page == .code {
            clearCode()
        }
        drawCheck.cancel()
        if newPage != .code {
            field = nil
            hint = nil
            connect = nil
        }
        if newPage != .compare {
            match = nil
            differ = nil
            shown = nil
            reported = false
        }
        page = newPage
        panel.title = title
        panel.contentView = view
        content = view
        fit()
    }

    /// Show the window, at the front of its level. It takes the focus only when it was hidden, so
    /// a page that changes while the user reads Lucille's page leaves the focus there.
    private func present() {
        if !panel.isVisible {
            panel.center()
            NSApp.activate()
            panel.makeKeyAndOrderFront(nil)
        }
        panel.orderFrontRegardless()
    }

    /// Size the window to its page, keeping its top left corner where it is while it shows.
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

    // MARK: What the user does

    private func codeChanged(_ text: String) {
        let words = Pairing.hint(for: text) ?? ""
        connect?.isEnabled = Pairing.looksLikeCode(Pairing.cleaned(text))
        if let hint, hint.stringValue != words {
            hint.stringValue = words
            fit()
        }
    }

    /// Report the number drawn once the window shows it: visible, and not wholly hidden by other
    /// windows. `occlusionState` says only that some part of the window shows, not which part, so
    /// a window over the number alone does not count as hiding it. Until then, look again every
    /// half second, and whenever what covers it changes.
    private func checkDrawn() {
        guard page == .compare, let comparison = shown, !reported else {
            return
        }
        guard panel.isVisible, panel.occlusionState.contains(.visible) else {
            drawCheck.schedule(after: 0.5) { [weak self] in
                self?.checkDrawn()
            }
            return
        }
        reported = true
        onDrawn?(comparison.sas, ProcessInfo.processInfo.systemUptime)
    }

    /// When the click or key that pressed `button` happened, on the clock of `systemUptime`: the
    /// time of the mouse event inside the button, or of the Space key that pressed it. A press with
    /// no such event, through accessibility say, happens now.
    private func answerTime(for button: NSButton) -> TimeInterval {
        let now = ProcessInfo.processInfo.systemUptime
        guard let event = NSApp.currentEvent, event.window === panel else {
            return now
        }
        switch event.type {
        case .leftMouseDown, .leftMouseUp:
            let point = button.convert(event.locationInWindow, from: nil)
            return button.bounds.contains(point) ? event.timestamp : now
        case .keyDown, .keyUp:
            return event.charactersIgnoringModifiers == " " ? event.timestamp : now
        default:
            return now
        }
    }

    @objc private func connectPressed(_ sender: Any?) {
        guard let field else {
            return
        }
        onSubmit?(field.stringValue)
    }

    @objc private func closePressed(_ sender: Any?) {
        onClose?()
    }

    @objc private func stopPressed(_ sender: Any?) {
        onStopAsked?()
    }

    @objc private func cancelBusyPressed(_ sender: Any?) {
        onCancelBusy?()
    }

    @objc private func matchPressed(_ sender: Any?) {
        guard let match else {
            return
        }
        onMatch?(answerTime(for: match))
    }

    @objc private func differPressed(_ sender: Any?) {
        onDiffer?()
    }
}

/// The pairing window. Its close button asks the app, which closes it once the flow agrees, and it
/// sends Command-V, C, X, A and Z, and Shift-Command-Z, to the field being edited, as an Edit menu
/// would: the app has no Edit menu of its own. That paste is the user's, through the field; the
/// app's code never reads the pasteboard.
final class GatePanel: NSPanel {
    var onCloseAsked: (() -> Void)?

    override func performClose(_ sender: Any?) {
        onCloseAsked?()
    }

    override func performKeyEquivalent(with event: NSEvent) -> Bool {
        if event.type == .keyDown, let action = GatePanel.editAction(for: event),
           NSApp.sendAction(action, to: nil, from: self) {
            return true
        }
        return super.performKeyEquivalent(with: event)
    }

    private static func editAction(for event: NSEvent) -> Selector? {
        let flags = event.modifierFlags.intersection([.command, .shift, .option, .control])
        guard let key = event.charactersIgnoringModifiers?.lowercased() else {
            return nil
        }
        if flags == [.command] {
            switch key {
            case "x":
                return #selector(NSText.cut(_:))
            case "c":
                return #selector(NSText.copy(_:))
            case "v":
                return #selector(NSText.paste(_:))
            case "a":
                return #selector(NSText.selectAll(_:))
            case "z":
                return Selector(("undo:"))
            default:
                return nil
            }
        }
        if flags == [.command, .shift] && key == "z" {
            return Selector(("redo:"))
        }
        return nil
    }
}

/// The code field, which says each change of its text.
final class CodeField: NSTextField {
    var onChange: ((String) -> Void)?

    override func textDidChange(_ notification: Notification) {
        super.textDidChange(notification)
        onChange?(stringValue)
    }
}
