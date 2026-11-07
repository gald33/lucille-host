import Foundation

/// A note the app shows: a title, a sentence, and the host's own words as details, if any.
struct ConnectionNote: Equatable, Sendable {
    let title: String
    let text: String
    let detail: String?
}

/// Every sentence the app says about connecting to Lucille and about the host it runs. The app's
/// name is read from the bundle (`HostText.appName`), never spelled here. tests/test_macos_build.py
/// checks these against PRIVACY.md's list of what this project must never claim, and checks that
/// `refusals` and `appBugCodes` cover every error code the verbs the app runs can give.
enum ConnectionText {
    private static let appName = HostText.appName

    // MARK: The menu

    static let notConnected = "Not connected to Lucille"
    static let connecting = "Connecting to Lucille…"
    static let connectedWaiting = "Connected, waiting for Lucille"
    static let connected = "Connected to Lucille"
    static let startingAgain = "\(appName)'s helper program stopped. Starting it again…"
    static let finishingEarlier = "Finishing a task from before…"
    static let stopping = "Finishing the current task…"
    static let needsAttention = "Needs attention"
    static let pausedSuffix = " · Paused"

    static let connectItem = "Connect to Lucille…"
    static let finishItem = "Finish Connecting…"
    static let stopConnectingItem = "Stop Connecting…"
    static let disconnectItem = "Disconnect from Lucille…"
    /// The Abilities menu's line while connected and `host.describe` is on.
    static let describeConnected = "Lucille sees which abilities are on and your folders' names"

    /// The status line, with " · Paused" while Lucille is paused.
    static func statusLine(_ health: Health, paused: Bool) -> String {
        let line: String
        switch health {
        case .notConnected:
            line = notConnected
        case .connecting:
            line = connecting
        case .startingAgain:
            line = startingAgain
        case .finishingEarlier:
            line = finishingEarlier
        case .stopping:
            line = stopping
        case .needsAttention:
            line = needsAttention
        case .connectedWaiting:
            line = connectedWaiting
        case .connected:
            line = connected
        }
        return paused ? line + pausedSuffix : line
    }

    /// The line under the status line, when there is one: what needs attention, or why the Mac is
    /// not connected.
    static func reason(_ health: Health) -> String? {
        switch health {
        case .needsAttention(let found):
            return problem(found)
        case .notConnected(let why?):
            return notConnectedReason(why)
        default:
            return nil
        }
    }

    // MARK: The code window

    static let codeTitle = "Connect to Lucille"
    static let codeSteps = "In Lucille, open Computers and add this computer. Copy the code Lucille shows, then paste it below."
    static let codePasteHow = "To paste, press Command-V, or Control-click the box and choose Paste."
    static let codeWarning = "Paste only a code you copied from Lucille's page yourself. Never paste one someone sent you."
    static let codePlaceholder = "Code from Lucille"
    static let codeHint = "That doesn't look like a code from Lucille. Copy it again from Lucille's page."
    /// Shown when folders were added or an ability other than `host.describe` is on: pairing sets
    /// the policy aside (spec/CLI.md §6, `policy_reset`).
    static let settingsSetAside = "Connecting starts \(appName)'s settings afresh: the folders you added and the abilities you turned on are set aside. You can add them again once connected."
    static let connectButton = "Connect"
    static let cancelButton = "Cancel"

    // MARK: The waiting page

    static let waitingTitle = "Waiting for Lucille…"
    static let waitingKeepOpen = "Keep Lucille's page open."
    static let waitingClockWrong = "Your Mac's clock is wrong, so Lucille turned away this Mac's answer. Set the clock right in System Settings, General, Date & Time; \(appName) keeps trying."
    static let stopQuestionTitle = "Stop connecting?"
    static let stopQuestionText = "Nothing is connected yet."
    static let stopConnectingButton = "Stop Connecting"
    static let keepWaitingButton = "Keep Waiting"

    // MARK: The compare page

    static let compareTitle = "Compare the numbers"
    static let compareQuestion = "Does Lucille's page show this number?"
    static let compareRule = "Click They Match only if Lucille's page shows exactly the same number. If the numbers differ, this Mac may be connecting to someone else."
    /// On the compare page of a pair the user already said differs.
    static let compareDiffered = "You said these numbers differ, so \(appName) won't confirm them. Click They Differ to stop connecting."
    static let theyMatch = "They Match"
    static let theyDiffer = "They Differ"
    static let checking = "Checking…"
    static let confirming = "Confirming…"
    static let turningDown = "Turning the connection down…"

    /// When the SAS must be confirmed by, in local time.
    static func answerBy(_ deadline: Date) -> String {
        "Answer by \(localTime(deadline)). After that, nothing is connected, and you start again in Lucille."
    }

    // MARK: Notes after pairing

    static let couldNotConnect = "\(appName) could not connect."
    static let confirmedTitle = "Confirmed on this Mac"
    static let confirmedText = "Now confirm on Lucille's page too. \(appName)'s menu says Connected once Lucille is ready."
    static let nothingConnectedTitle = "Nothing was connected"
    static let differedTold = "This Mac turned the connection down, and Lucille was told. Numbers that differ can mean that someone else made the code, or intercepted it. To try again, in Lucille, open Computers and add this computer, then paste the new code."
    static let differedNotTold = "This Mac turned the connection down, but Lucille wasn't reachable, so in Lucille, open Computers and remove this computer. Numbers that differ can mean that someone else made the code, or intercepted it."
    static let stoppedTold = "This Mac stopped connecting, and Lucille was told."
    static let stoppedNotTold = "This Mac stopped connecting, but Lucille wasn't reachable, so in Lucille, open Computers and remove this computer."
    static let couldNotStop = "\(appName) could not stop connecting."
    static let stopUnknown = "\(appName) couldn't tell whether connecting stopped. Choose Stop Connecting… to try again."
    static let confirmedAnotherTitle = "A different connection was confirmed"
    static let confirmedAnotherText = "The connection now confirmed on this Mac isn't the one \(appName) was setting up. Choose Disconnect from Lucille…, then connect again."
    static let notConfirmedTitle = "This Mac wasn't confirmed"
    static let notConfirmedText = "Choose Finish Connecting… to compare the numbers again."
    static let confirmUnknownTitle = "\(appName) couldn't tell whether this Mac was confirmed"
    static let confirmUnknownText = "\(appName)'s menu shows where things are. If it offers Finish Connecting…, choose it to compare the numbers again."
    static let damaged = "\(appName) is damaged. Download it again."

    /// The sentence for each error code the pairing verbs, `forget`, `status`, `run` and
    /// `policy reset` can give (spec/CLI.md §7 and §10), except the codes in `appBugCodes`, which
    /// all read as `appBug`, and `waiting` and `sas_ready`, after which the app asks `pair status`
    /// and draws the SAS (`Pairing.redrawCodes`). `expired` here is the code expiring before a SAS
    /// was shown; after one was shown it is `expiredAfterSAS`. Any other code reads as the host's
    /// own sentence.
    static let refusals: [String: String] = [
        "bad_code": "That isn't a code from Lucille. Copy it again from Lucille's page.",
        "wrong_hub": "This code isn't from Lucille. Nothing was connected.",
        "code_has_token": "This code isn't from Lucille. Nothing was connected.",
        "held": "This Mac already has a connection to Lucille, or one being set up. Choose Stop Connecting… or Disconnect from Lucille… first.",
        "busy": "An earlier attempt is still finishing. Try again in a minute.",
        "lock_failed": "\(appName) couldn't lock its settings, so nothing was connected. Try again.",
        "policy_lock_failed": "\(appName) couldn't lock its settings, so nothing was connected. Try again.",
        // Reset Settings takes it as done: there was no invalid file to set aside.
        "not_invalid": "\(appName)'s settings can be read again, so nothing was changed.",
        "answer_failed": "\(appName) couldn't answer this code. Nothing was connected. Copy a new code from Lucille and try again.",
        "hub_error": "\(appName) couldn't reach Lucille. Check that this Mac is online, then try again. You can use the same code.",
        "leaked": "This code may have been used by someone else. Nothing was connected. In Lucille, remove this computer, add it again, and share the new code with nobody.",
        "expired": "The code expired. Nothing was connected. In Lucille, open Computers and add this computer again.",
        "ended": "Lucille ended this connection. Nothing was connected.",
        "none": "The connection was ended on this Mac meanwhile. Nothing was connected.",
        "interrupted": "Connecting was paused. Choose Finish Connecting… to go on.",
        "no_gate": "This connection can't be finished on this Mac. Choose Stop Connecting…, then connect again.",
        "pair_unusable": "\(appName) can't use its connection to Lucille. Choose Disconnect from Lucille…, then connect again. If Disconnect doesn't work either, \(appName) can't fix this itself: in Lucille, open Computers and remove this computer.",
        "several_pairs": "\(appName) holds more than one connection to Lucille, so it uses neither. \(appName) can't end them: Disconnect and Stop Connecting refuse too. They can only be removed by hand: see \u{201C}More than one connection\u{201D} in macos/README.md.",
        "room_not_left": "\(appName) couldn't finish ending an earlier connection. Try again.",
        "already_confirmed": "This Mac was already confirmed. To end it, choose Disconnect from Lucille…",
        "insecure_keyring": "macOS's Keychain can't keep \(appName)'s connection safely, so \(appName) won't connect.",
        "store_error": "\(appName) can't open its Keychain item. If macOS asked about the Keychain, click Allow, then try again. If this keeps happening, restart your Mac.",
    ]

    /// Codes that can only be the app's own mistake against the host it ships, or a host that
    /// cannot start here: each reads as `appBug`, with the host's words as details.
    static let appBugCodes: Set<String> = [
        "usage", "internal", "bad_profile", "unsupported_platform", "os_error", "code_in_argv",
        "bad_hub", "bad_options", "no_code", "startup_refused", "runtime_folder", "state_invalid",
        // `log --limit` takes 1 or more, and the app always asks for 200.
        "bad_limit",
    ]

    static let expiredAfterSAS = "The time to compare the numbers ran out. Nothing was connected. Start again in Lucille."
    static let appBug = "Something went wrong in \(appName)'s helper program."

    /// The sentence for a refusal. `afterSAS` is whether a SAS was shown in this flow.
    static func refusal(code: String, message: String?, afterSAS: Bool) -> String {
        if code == "expired" && afterSAS {
            return expiredAfterSAS
        }
        if let sentence = refusals[code] {
            return sentence
        }
        if appBugCodes.contains(code) {
            return appBug
        }
        // An error code is open (spec/CLI.md §12): show the host's own sentence.
        if let message, !message.isEmpty {
            return message
        }
        return appBug
    }

    static func note(_ note: PairingNote) -> ConnectionNote {
        switch note {
        case .refused(let code, let message, let afterSAS):
            let text = refusal(code: code, message: message, afterSAS: afterSAS)
            return ConnectionNote(title: couldNotConnect, text: text, detail: message == text ? nil : message)
        case .confirmed:
            return ConnectionNote(title: confirmedTitle, text: confirmedText, detail: nil)
        case .confirmedAnother:
            return ConnectionNote(title: confirmedAnotherTitle, text: confirmedAnotherText, detail: nil)
        case .notConfirmed:
            return ConnectionNote(title: notConfirmedTitle, text: notConfirmedText, detail: nil)
        case .confirmUnknown:
            return ConnectionNote(title: confirmUnknownTitle, text: confirmUnknownText, detail: nil)
        case .rejected(let told, let afterDiffer):
            let text: String
            if afterDiffer {
                text = told ? differedTold : differedNotTold
            } else {
                text = told ? stoppedTold : stoppedNotTold
            }
            return ConnectionNote(title: nothingConnectedTitle, text: text, detail: nil)
        case .rejectFailed(let error?):
            let text = refusal(code: error.code, message: error.message, afterSAS: false)
            return ConnectionNote(title: couldNotStop, text: text, detail: error.message == text ? nil : error.message)
        case .rejectFailed(nil):
            return ConnectionNote(title: couldNotStop, text: stopUnknown, detail: nil)
        case .notStarted:
            return ConnectionNote(title: couldNotConnect, text: damaged, detail: nil)
        }
    }

    // MARK: Disconnect

    static let disconnectQuestionTitle = "Disconnect this Mac from Lucille?"
    static let disconnectQuestionText = "This Mac will stop answering Lucille. Your folders and abilities stay on this Mac, but a new connection starts without them."
    static let disconnectButton = "Disconnect"
    static let disconnecting = "Disconnecting…"
    static let disconnectedTitle = "Disconnected"
    static let disconnectedTold = "Disconnected. Lucille was told."
    static let disconnectedNotTold = "Disconnected on this Mac. Lucille wasn't reachable, so in Lucille, open Computers and remove this Mac."
    static let disconnectedLeft = "Disconnected."
    static let wasNotConnected = "This Mac wasn't connected."
    static let couldNotDisconnect = "\(appName) could not disconnect."
    static let disconnectRoomNotLeft = "\(appName) couldn't finish disconnecting. Try again."
    static let disconnectUnknown = "\(appName) couldn't tell whether this Mac was disconnected. Try again."
    /// Added when a Disconnect's drain reached its bound and ran `kill`, which pauses the host.
    static let pausedByDisconnect = "A task was stopped so that this Mac could disconnect, so \(appName) is paused. Choose Resume Lucille to resume."

    /// What the app says once `forget --json` has run. `forget`'s `none` means there was nothing
    /// to forget, which a disconnecting shell takes as done (spec/CLI.md §7).
    static func disconnected(_ outcome: VerbOutcome) -> ConnectionNote {
        switch outcome {
        case .done:
            switch outcome.forget {
            case .forgotten(_, let told)?:
                return ConnectionNote(title: disconnectedTitle, text: told ? disconnectedTold : disconnectedNotTold, detail: nil)
            case .left?:
                return ConnectionNote(title: disconnectedTitle, text: disconnectedLeft, detail: nil)
            case .other?, nil:
                return ConnectionNote(title: couldNotDisconnect, text: disconnectUnknown, detail: nil)
            }
        case .refused(let error):
            if error.code == "none" {
                return ConnectionNote(title: disconnectedTitle, text: wasNotConnected, detail: nil)
            }
            if error.code == "room_not_left" {
                return ConnectionNote(title: couldNotDisconnect, text: disconnectRoomNotLeft, detail: error.message)
            }
            let text = refusal(code: error.code, message: error.message, afterSAS: false)
            return ConnectionNote(title: couldNotDisconnect, text: text, detail: error.message == text ? nil : error.message)
        default:
            return ConnectionNote(title: couldNotDisconnect, text: disconnectUnknown, detail: nil)
        }
    }

    // MARK: Reset Settings

    /// Offered in the note behind the menu's line that says the settings file can't be read, and
    /// in the one behind `run`'s Needs attention line for it (decisions/0002 §14).
    static let resetSettingsItem = "Reset Settings…"
    static let resetQuestionTitle = "Reset \(appName)'s settings?"
    /// The fresh policy has only `host.describe` on, and no folder (spec/CLI.md §6, `policy_reset`).
    static let resetQuestionText = "\(appName) will set its settings file aside and start afresh, with no folders and only the ability that tells Lucille about this Mac. Until you add your folders and abilities again, Lucille's requests that need them are turned away."
    static let resetButton = "Reset Settings"
    static let resetting = "Resetting \(appName)'s settings…"
    static let resetTitle = "Settings reset"
    static let resetText = "\(appName)'s settings start afresh. The old file was set aside and isn't used. Add your folders and abilities again from \(appName)'s menu."
    static let nothingToResetTitle = "Nothing needed resetting"
    static let couldNotReset = "\(appName) couldn't reset its settings."
    /// `policy_lock_failed` from `policy reset`: the pairing verbs' sentence for it says that
    /// nothing was connected, which is not what happened here.
    static let resetLockFailed = "\(appName) couldn't lock its settings file, so nothing was changed. Try again."
    static let resetUnknown = "\(appName) couldn't tell whether its settings were reset. If its menu still says that its settings file can't be read, choose that line to try again."

    /// What the app says once `policy reset --json` has run. `earlierRunStuck` is whether a run
    /// the app stopped waiting for has still not ended (`HostCLI.stuckRuns`), which may hold the
    /// policy lock.
    static func reset(_ outcome: ResetOutcome, earlierRunStuck: Bool = false) -> ConnectionNote {
        switch outcome {
        case .reset:
            return ConnectionNote(title: resetTitle, text: resetText, detail: nil)
        case .nothingToReset:
            return ConnectionNote(title: nothingToResetTitle, text: refusals["not_invalid"] ?? appBug, detail: nil)
        case .failed(let verb):
            switch verb {
            case .refused(let error) where error.code == "policy_lock_failed":
                return ConnectionNote(title: couldNotReset, text: resetLockFailed, detail: error.message)
            case .refused(let error):
                let text = refusal(code: error.code, message: error.message, afterSAS: false)
                return ConnectionNote(title: couldNotReset, text: text, detail: error.message == text ? nil : error.message)
            case .notStarted:
                return ConnectionNote(title: couldNotReset, text: damaged, detail: nil)
            case .notFinished:
                return ConnectionNote(title: couldNotReset, text: HostText.stuckRun, detail: nil)
            case .timedOut, .cancelled:
                return ConnectionNote(title: couldNotReset, text: earlierRunStuck ? HostText.stuckRun : resetUnknown, detail: nil)
            case .done, .signaled, .otherExit:
                return ConnectionNote(title: couldNotReset, text: resetUnknown, detail: nil)
            }
        }
    }

    // MARK: Quit, log out, paused

    static let quitQuestionTitle = "Quit \(appName)?"
    static let quitQuestionText = "Your Mac will stop answering Lucille until you open \(appName) again."
    static let quitButton = "Quit"
    static let finishing = "Finishing the current task…"
    static let stopNow = "Stop now (\(appName) will be paused when you next open it)"
    static let stoppingNow = "Stopping…"
    static let finishingLogout = "If it isn't done in 20 seconds, \(appName) stops it, and will be paused when you next open it."
    static let pausedTitle = "\(appName) is paused"
    static let pausedText = "Lucille's requests are turned away until you resume. You paused \(appName) or chose Stop What's Running, or a task was stopped when \(appName) quit or you logged out."
    static let resumeButton = "Resume"
    static let notNowButton = "Not Now"
    static let keepsStoppingTitle = "\(appName) keeps stopping"
    static let keepsStoppingText = "\(appName)'s helper program stopped 5 times in 10 minutes, so \(appName) stopped starting it. Lucille's requests wait, and fail if this Mac doesn't answer in time."
    static let tryAgainButton = "Try Again"
    static let okButton = "OK"
    static let showDetails = "Show Details"
    static let hideDetails = "Hide Details"

    // MARK: Stop What's Running

    /// Shown above Pause and Resume while an operation's process runs (decisions/0002 §14).
    static let stopWhatsRunningItem = "Stop What's Running"
    static let stoppedAndPausedTitle = "Stopped, and paused. Resume when you're ready."
    static let stoppedAndPausedText = "Lucille's requests are turned away until you choose Resume Lucille."
    static let couldNotStopWhatsRunning = "\(appName) couldn't stop what's running."
    static let stopWhatsRunningUnknown = "It may still be running. Choose Stop What's Running again, or quit \(appName) and choose Stop now."

    // MARK: Activity

    static let activityItem = "Activity…"
    static let activityTitle = "Activity"
    /// What the Activity window keeps, and for how long (decisions/0002 §15).
    static let activityHeader = "What Lucille asked this Mac to do since \(appName) opened. \(appName) keeps this only while it is open."
    static let activityEmpty = "Lucille hasn't asked this Mac to do anything since \(appName) opened."
    static let showLog = "Show the Host's Log"
    static let hideLog = "Hide the Host's Log"
    /// Above the host's log, which is shown as the host wrote it: the log is the user's own file,
    /// and proves nothing (spec/CLI.md §6, `log_line`).
    static let logCaption = "As the host wrote it. Anything running as you can change this file."
    static let readingLog = "Reading the host's log…"
    static let logEmpty = "The host's log is empty."
    static let couldNotReadLog = "\(appName) couldn't read the host's log."

    // MARK: Needs attention

    static let clockDirections = "Open System Settings, choose General, then Date & Time, and set the time automatically."
    static let notResponding = "\(appName)'s helper program isn't responding."
    static let policyInvalid = "\(appName)'s settings can't be read, so it is doing nothing."
    static let policyStuck = "A folder check is stuck. Reconnect the drive or restart your Mac."
    static let hubNeverReached = "\(appName) hasn't reached Lucille yet. Check that this Mac is online."
    static let readStuck = "A message from Lucille can't be opened, so \(appName) has stopped reading Lucille's requests. Disconnect, then connect again."
    static let leftCheck3 = "\(appName) left its connection to Lucille because Lucille's offer named a different server from the code. Nothing runs until you connect again."
    static let leftCheck4 = "\(appName) left its connection to Lucille because Lucille's offer carried a key \(appName) doesn't accept. Nothing runs until you connect again."
    static let leftBefore = "\(appName) left its connection to Lucille because it had left it before. Nothing runs until you connect again."
    static let leftOther = "\(appName) left its connection to Lucille. Nothing runs until you connect again."
    static let anotherCopy = "Another copy of \(appName)'s helper program is running, so this one can't start. Quit any other copy of \(appName). If this keeps happening, restart your Mac."
    static let startupRefused = "\(appName)'s helper program refused to start on this Mac."
    static let downgrade = "This \(appName) is older than one that ran on this Mac before, so it won't start. Use the newer \(appName)."
    static let statusNoAnswer = "\(appName)'s helper program didn't answer in time. If macOS asked about the Keychain, click Allow, then choose Try Again."
    /// Shown once `status` has taken a few seconds: macOS may be asking the user whether the app
    /// may open its Keychain item, and the verb waits for that answer.
    static let keychainWait = "Waiting for \(appName)'s helper program. If macOS asks about \(appName)'s Keychain item, click Allow."
    static let leakedWhileRunning = "This connection's code may have been used by someone else, so nothing runs. Connect again with a new code."
    static let expiredWhileRunning = "The time to compare the numbers ran out, so nothing was connected. Connect again."
    static let endedInLucille = "This Mac was disconnected in Lucille."
    static let forgottenElsewhere = "This Mac was disconnected."

    /// The clock's problem: how far off, when the host knows (`skew` is positive when this Mac's
    /// clock is behind), then what to do. Words only: the app opens no settings itself.
    static func clock(skew: Double?) -> String {
        guard let skew else {
            return "Your Mac's clock is more than 5 minutes off, so \(appName) can't answer Lucille. " + clockDirections
        }
        let minutes = max(1, Int((abs(skew) / 60).rounded()))
        let unit = minutes == 1 ? "minute" : "minutes"
        let way = skew > 0 ? "behind" : "ahead"
        return "Your Mac's clock is \(minutes) \(unit) \(way), so \(appName) can't answer Lucille. " + clockDirections
    }

    static func hubUnreachable(since: Date) -> String {
        "\(appName) hasn't reached Lucille since \(localTime(since)). Check that this Mac is online."
    }

    static func hostTooOld(minVersion: String, hostVersion: String) -> String {
        "Lucille asked for something this \(appName) is too old to do. It needs \(appName) \(minVersion) or newer; this is \(hostVersion)."
    }

    static func problem(_ problem: Problem) -> String {
        switch problem {
        case .halted(let found):
            return attention(found)
        case .notResponding:
            return notResponding
        case .policyInvalid:
            return policyInvalid
        case .policyStuck:
            return policyStuck
        case .clock(let skew):
            return clock(skew: skew)
        case .hubUnreachable(let since?):
            return hubUnreachable(since: since)
        case .hubUnreachable(nil):
            return hubNeverReached
        case .readStuck:
            return readStuck
        case .leftConnection(let because):
            switch because {
            case "check_3":
                return leftCheck3
            case "check_4":
                return leftCheck4
            case "left_before":
                return leftBefore
            default:
                return leftOther
            }
        }
    }

    static func attention(_ attention: Attention) -> String {
        switch attention {
        case .keepsStopping:
            return keepsStoppingTitle
        case .anotherCopy:
            return anotherCopy
        case .insecureKeyring:
            return refusals["insecure_keyring"] ?? appBug
        case .storeError:
            return refusals["store_error"] ?? appBug
        case .startupRefused:
            return startupRefused
        case .downgrade:
            return downgrade
        case .appBug, .contract:
            return appBug
        case .severalPairs:
            return refusals["several_pairs"] ?? appBug
        case .pairUnusable:
            return refusals["pair_unusable"] ?? appBug
        case .statusFailed(let code?):
            return refusal(code: code, message: nil, afterSAS: false)
        case .statusFailed(nil):
            return statusNoAnswer
        }
    }

    /// Why the Mac is not connected, when that needs saying; nil for a Mac that was never paired.
    static func notConnectedReason(_ why: NotConnected) -> String? {
        switch why {
        case .notPaired, .other:
            return nil
        case .noGate:
            return refusals["no_gate"]
        case .leaked:
            return leakedWhileRunning
        case .expired:
            return expiredWhileRunning
        case .ended:
            return endedInLucille
        case .forgotten:
            return forgottenElsewhere
        }
    }

    /// A time in the user's own format: weekday, day, month, and time of day.
    static func localTime(_ date: Date) -> String {
        let formatter = DateFormatter()
        formatter.setLocalizedDateFormatFromTemplate("EEEEdMMMMjmm")
        return formatter.string(from: date)
    }
}
