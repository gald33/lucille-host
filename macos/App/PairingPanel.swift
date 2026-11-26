import AppKit

/// The window the app pairs in, on decisions/0001's gate, for Connect from Lucille's chat: a page
/// for the chat account, one with the connect code to send, one while it waits for Lucille, one to
/// compare the numbers, and one while a verb it asked for runs. It shows what
/// `PairingFlow`'s screen says and tells the app what the user did; it decides nothing itself.
///
/// - It is one non-modal panel that stays on the screen while the user looks at Lucille's page in
///   another app, above other windows.
/// - The user types or pastes their username into its field: the app never reads the pasteboard,
///   and writes nothing to it. Command-V, C, X, A and Z reach the field through the panel, as an
///   Edit menu would send them; the connect code is selectable, for the user to copy.
/// - The number counts as drawn only once it is on the screen: the panel visible and not wholly
///   hidden by other windows (macOS's occlusion state says only that some part of it shows), a
///   moment after it was set. They Match and They Differ stay off until then, neither is a
///   default button, and the compare page starts with neither focused.
/// - A click on They Match carries its event's time, so the flow can refuse a click made before
///   the number was drawn.
/// - Its views sit straight in the window, in no group, as in a note.
/// - It is the window's delegate, so every close AppKit tells the delegate about reaches the flow;
///   where AppKit asks first, the flow decides.
@MainActor
final class PairingPanel: NSObject, NSWindowDelegate {
    /// What the user did. The app's glue turns each into an input of `PairingFlow`.
    /// Continue on the account page: the service chosen, and the username as typed.
    var onSubmitAccount: ((String, String) -> Void)?
    /// The window's close button, or Cancel on the account page; or the window closing on a route
    /// that asked nobody first.
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
        case account
        case starting
        case connectCode(ConnectRequest)
        case waiting(clockSkewed: Bool, account: SignedAccount?)
        case compare
        case busy(PairingFlow.Busy)
    }

    private let width: CGFloat = 460
    private let panel: GatePanel
    private var page: Page = .none
    private var content: NSView
    private var services: NSPopUpButton?
    private var username: CodeField?
    private var usernameHint: NSTextField?
    private var proceed: NSButton?
    /// The account Lucille signed, which the compare page shows above the number.
    private var account: SignedAccount?
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
        // Not restorable: macOS would keep its state in a folder named after the app.
        panel.isRestorable = false
        panel.title = ConnectionText.codeTitle
        panel.onCloseAsked = { [weak self] in
            self?.onClose?()
        }
        panel.delegate = self
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
        clearUsername()
        drawCheck.cancel()
        page = .none
        shown = nil
        reported = false
        panel.orderOut(nil)
    }

    /// The account page: the chat service, and the user's own username there. `setAside` adds the
    /// sentence that connecting sets the folders and abilities aside.
    func showAccount(setAside: Bool) {
        let fresh = page != .account
        if fresh {
            install(accountPage(setAside: setAside), title: ConnectionText.codeTitle, page: .account)
            panel.initialFirstResponder = username
        }
        present()
        if fresh, let username {
            _ = panel.makeFirstResponder(username)
        }
    }

    func showStarting() {
        if page != .starting {
            install(startingPage(), title: ConnectionText.codeTitle, page: .starting)
        }
        present()
    }

    /// The connect code to send Lucille, while the request is live.
    func showConnectCode(_ request: ConnectRequest) {
        if page != .connectCode(request) {
            install(connectCodePage(request), title: ConnectionText.codeTitle, page: .connectCode(request))
            panel.initialFirstResponder = nil
            _ = panel.makeFirstResponder(nil)
        }
        present()
    }

    /// The waiting page; with `account`, the account Lucille signed for this Connect.
    func showWaiting(clockSkewed: Bool, account signed: SignedAccount? = nil) {
        account = signed
        let wanted = Page.waiting(clockSkewed: clockSkewed, account: signed)
        if page != wanted {
            install(waitingPage(clockSkewed: clockSkewed, account: signed), title: ConnectionText.codeTitle, page: wanted)
        }
        present()
    }

    /// The compare page. A new number, or one the flow wants drawn again, is reported drawn once
    /// it is on the screen; until the flow has that, both buttons are off.
    func showCompare(_ comparison: PairingFlow.Comparison, account signed: SignedAccount? = nil) {
        if signed != account {
            account = signed
            shown = nil
        }
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

    /// Empty the username field, and what the window's field editor and its undo still hold of it.
    func clearUsername() {
        guard let username else {
            return
        }
        panel.endEditing(for: nil)
        username.stringValue = ""
        panel.fieldEditor(false, for: nil)?.string = ""
        panel.undoManager?.removeAllActions()
        usernameChanged("")
    }

    // MARK: Pages

    private func accountPage(setAside: Bool) -> NSView {
        let intro = label(ConnectionText.accountIntro)
        intro.font = .boldSystemFont(ofSize: NSFont.systemFontSize)
        let services = NSPopUpButton(frame: .zero, pullsDown: false)
        for service in Connect.services {
            services.addItem(withTitle: Connect.serviceName(service))
            services.lastItem?.representedObject = service
        }
        services.setAccessibilityLabel(ConnectionText.serviceLabel)
        services.target = self
        services.action = #selector(serviceChosen(_:))
        let service = Connect.services[0]
        let prompt = label(ConnectionText.usernamePrompt(service: service))
        let field = CodeField(string: "")
        field.placeholderString = ConnectionText.usernamePlaceholder
        field.setAccessibilityLabel(ConnectionText.usernamePrompt(service: service))
        field.usesSingleLineMode = true
        field.cell?.wraps = false
        field.cell?.isScrollable = true
        field.onChange = { [weak self] text in
            self?.usernameChanged(text)
        }
        let hint = label("", secondary: true)
        let needed = label(ConnectionText.usernameNeeded(service: service), secondary: true)
        let cancel = NSButton(title: ConnectionText.cancelButton, target: self, action: #selector(closePressed(_:)))
        cancel.keyEquivalent = "\u{1b}"
        let proceed = NSButton(title: ConnectionText.continueButton, target: self, action: #selector(continuePressed(_:)))
        proceed.keyEquivalent = "\r"
        proceed.isEnabled = false
        var rows: [NSView] = [intro, services, prompt, field, hint, needed]
        if setAside {
            rows.append(label(ConnectionText.settingsSetAside))
        }
        self.services = services
        self.username = field
        self.usernameHint = hint
        self.proceed = proceed
        return layout(rows, buttons: [cancel, proceed])
    }

    private func startingPage() -> NSView {
        let cancel = NSButton(title: ConnectionText.cancelButton, target: self, action: #selector(stopPressed(_:)))
        cancel.keyEquivalent = "\u{1b}"
        return layout([spinnerRow(ConnectionText.gettingCode)], buttons: [cancel])
    }

    private func connectCodePage(_ request: ConnectRequest) -> NSView {
        let send = label(ConnectionText.sendThis(code: request.code))
        send.font = .boldSystemFont(ofSize: NSFont.systemFontSize)
        // The line to send, which the user may select and copy themselves: the app writes nothing
        // to the pasteboard.
        let line = NSTextField(labelWithString: ConnectionText.sendLine(code: request.code))
        line.font = .monospacedSystemFont(ofSize: 26, weight: .semibold)
        line.alignment = .center
        line.isSelectable = true
        let only = label(ConnectionText.sendOnlyToLucille)
        let expires = label(ConnectionText.codeExpires(request.expiresAt), secondary: true)
        let waiting = spinnerRow(ConnectionText.waitingFor(username: request.username, service: request.service))
        let cancel = NSButton(title: ConnectionText.cancelButton, target: self, action: #selector(stopPressed(_:)))
        cancel.keyEquivalent = "\u{1b}"
        return layout([send, line, only, expires, waiting], buttons: [cancel])
    }

    private func waitingPage(clockSkewed: Bool, account signed: SignedAccount?) -> NSView {
        let title = label(ConnectionText.waitingTitle)
        title.font = .boldSystemFont(ofSize: NSFont.systemFontSize)
        var rows: [NSView] = []
        if let signed {
            let who = label(ConnectionText.connectingAs(signed))
            who.font = .boldSystemFont(ofSize: NSFont.systemFontSize)
            rows.append(who)
        }
        rows += [title, label(ConnectionText.waitingNumberInChat)]
        if clockSkewed {
            rows.append(label(ConnectionText.waitingClockWrong))
        }
        let cancel = NSButton(title: ConnectionText.cancelButton, target: self, action: #selector(stopPressed(_:)))
        cancel.keyEquivalent = "\u{1b}"
        return layout(rows, buttons: [cancel])
    }

    private func comparePage(_ comparison: PairingFlow.Comparison) -> NSView {
        var rows: [NSView] = []
        if let account {
            rows.append(label(ConnectionText.connectingAs(account)))
        }
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
        return layout(rows + [question, number, rule, deadline], buttons: [differ, match])
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
        case .cancelling:
            words = ConnectionText.cancelling
        }
        let cancel = NSButton(title: ConnectionText.cancelButton, target: self, action: #selector(cancelBusyPressed(_:)))
        cancel.keyEquivalent = "\u{1b}"
        return layout([spinnerRow(words)], buttons: [cancel])
    }

    /// A small spinner, then `words`.
    private func spinnerRow(_ words: String) -> NSView {
        let row = NSView()
        let spinner = NSProgressIndicator()
        spinner.style = .spinning
        spinner.controlSize = .small
        spinner.isIndeterminate = true
        spinner.startAnimation(nil)
        let text = NSTextField(wrappingLabelWithString: words)
        text.preferredMaxLayoutWidth = width - 70
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
        return row
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
        if page == .account {
            clearUsername()
        }
        drawCheck.cancel()
        if newPage != .account {
            services = nil
            username = nil
            usernameHint = nil
            proceed = nil
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

    /// The service chosen on the account page, as the host names it.
    private var chosenService: String {
        (services?.selectedItem?.representedObject as? String) ?? Connect.services[0]
    }

    private func usernameChanged(_ text: String) {
        let words = Connect.hint(for: text, service: chosenService) ?? ""
        proceed?.isEnabled = Connect.normalizedUsername(text, service: chosenService) != nil
        if let usernameHint, usernameHint.stringValue != words {
            usernameHint.stringValue = words
            fit()
        }
    }

    @objc private func serviceChosen(_ sender: Any?) {
        usernameChanged(username?.stringValue ?? "")
    }

    @objc private func continuePressed(_ sender: Any?) {
        guard let username else {
            return
        }
        onSubmitAccount?(chosenService, username.stringValue)
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

    // MARK: Closing the window

    /// The title bar's close button. In the pairing check on macOS 15, pressed through
    /// accessibility, it closed the window without going through `GatePanel.performClose(_:)`.
    /// AppKit asks here before a user closes the window: the flow decides, and `hide()` orders the
    /// window out once it agrees.
    func windowShouldClose(_ sender: NSWindow) -> Bool {
        onClose?()
        return false
    }

    /// A close that asked neither `GatePanel.performClose(_:)` nor `windowShouldClose(_:)`: tell
    /// the flow, so it never takes a closed window for an open one. While a verb runs the flow
    /// keeps its page, and the window comes back the next time the flow shows a page.
    func windowWillClose(_ notification: Notification) {
        guard page != .none else {
            return
        }
        onClose?()
    }
}

/// The pairing window. A close asks the app, which hides the window once the flow agrees:
/// `performClose(_:)` here, and the title bar's close button, as AppKit documents it, through
/// `PairingPanel.windowShouldClose(_:)` (expected, not yet seen on a Mac). It sends Command-V, C, X, A and Z, and Shift-Command-Z,
/// to the field being edited, as an Edit menu would: the app has no Edit menu of its own. That
/// paste is the user's, through the field; the app's code never reads the pasteboard.
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
