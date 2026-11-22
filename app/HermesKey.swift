// HermesKey: long-press the 🌐 Globe / fn key to talk to Hermes, with a Siri-style HUD.
// - Listens (read-only event tap) for a long press of fn → talks to the voice worker.
// - Shows a floating Hermes HUD that animates from <data dir>/state.json
//   (written by ask.py: listening / thinking / speaking / idle + live mic level).
import Cocoa
import SwiftUI
import CoreGraphics
import UserNotifications

// Paths come from Info.plist (written by `hermes bubble install`): HBDataDir, HBRuntimeDir.
func plistPath(_ key: String, _ fallback: String) -> String {
    let v = (Bundle.main.object(forInfoDictionaryKey: key) as? String) ?? fallback
    return NSString(string: v).expandingTildeInPath
}
let base = plistPath("HBDataDir", "~/.hermes/bubble")
let runtimeDir = plistPath("HBRuntimeDir", "~/.hermes/plugins/hermes-bubble/runtime")
let launcher = runtimeDir + "/launch.sh"

/// Values from <data dir>/config.json (hold time, hermes binary).
func configValue(_ key: String) -> Any? {
    guard let d = FileManager.default.contents(atPath: base + "/config.json"),
          let j = try? JSONSerialization.jsonObject(with: d) as? [String: Any] else { return nil }
    return j[key]
}
let HOLD: Double = (configValue("hold_seconds") as? Double) ?? 0.45
let statePath = base + "/state.json"
let pidPath = base + "/ask.pid"
let convPath = base + "/conv.open"      // exists while the bubble/conversation is open
let notifyPath = base + "/notify.json"  // ask.py drops each answer here → clickable notification

func log(_ s: String) {
    let line = "\(Date()) \(s)\n"
    let path = base + "/fnkey.log"
    if let h = FileHandle(forWritingAtPath: path) {
        h.seekToEndOfFile(); h.write(line.data(using: .utf8)!); h.closeFile()
    } else {
        FileManager.default.createFile(atPath: path, contents: line.data(using: .utf8))
    }
}

// MARK: - HUD model

final class HUDModel: ObservableObject {
    @Published var state: String = "idle"     // starting | listening | thinking | speaking | error | idle
    @Published var level: Double = 0          // 0...1 mic level (listening)
    @Published var text: String = ""
    var lastChange = Date()
}

let model = HUDModel()

struct Waveform: View {
    let level: Double
    let state: String
    var body: some View {
        TimelineView(.animation) { tl in
            let t = tl.date.timeIntervalSinceReferenceDate
            HStack(spacing: 4) {
                ForEach(0..<7, id: \.self) { i in
                    Capsule()
                        .fill(LinearGradient(colors: [Color(red: 0.55, green: 0.75, blue: 1.0),
                                                      Color(red: 0.75, green: 0.55, blue: 1.0)],
                                             startPoint: .bottom, endPoint: .top))
                        .frame(width: 4, height: barHeight(i: i, t: t))
                }
            }
            .frame(height: 32)
        }
    }
    func barHeight(i: Int, t: Double) -> CGFloat {
        let phase = t * 6 + Double(i) * 0.9
        let amp: Double
        switch state {
        case "listening": amp = 0.15 + level * 0.85
        case "speaking": amp = 0.55
        case "thinking": amp = 0.18
        default: amp = 0.25
        }
        let wobble: Double = state == "thinking"
            ? 0.4 + 0.6 * (0.5 + 0.5 * sin(t * 4 - Double(i) * 0.7))
            : abs(0.55 + 0.45 * sin(phase) * cos(phase * 0.37 + Double(i)))
        return CGFloat(max(4, 30 * amp * wobble))
    }
}

struct HUDView: View {
    @ObservedObject var m: HUDModel
    var icon: NSImage?
    var label: String {
        switch m.state {
        case "starting": return "Hermes"
        case "listening": return "Listening…"
        case "thinking": return "Thinking…"
        case "speaking": return "Speaking…"
        case "error": return m.text.isEmpty ? "Sorry, I didn't catch that" : m.text
        case "ready": return "Hold 🌐 to keep talking"
        default: return "Hermes"
        }
    }
    func pulse(_ t: Double) -> Double {
        switch m.state {
        case "listening": return 1.0 + m.level * 0.35
        case "thinking": return 1.0 + 0.08 * sin(t * 5)
        case "speaking": return 1.0 + 0.1 * abs(sin(t * 7))
        case "ready": return 1.0
        default: return 1.0 + 0.05 * sin(t * 3)
        }
    }
    var body: some View {
        TimelineView(.animation) { tl in
            let t = tl.date.timeIntervalSinceReferenceDate
            HStack(alignment: .center, spacing: 14) {
                ZStack {
                    Circle()
                        .fill(AngularGradient(colors: [.blue, .purple, .pink, .cyan, .blue],
                                              center: .center, angle: .degrees(t * 90)))
                        .frame(width: 50, height: 50)
                        .scaleEffect(pulse(t))
                        .blur(radius: 6)
                        .opacity(m.state == "error" ? 0.3 : 0.9)
                    if let icon {
                        Image(nsImage: icon).resizable().interpolation(.high)
                            .frame(width: 44, height: 44)
                            .clipShape(Circle())
                            .overlay(Circle().stroke(Color.white.opacity(0.35), lineWidth: 1))
                    } else {
                        Text("☤").font(.system(size: 26)).foregroundColor(.white)
                    }
                }
                .frame(width: 62, height: 62)
                VStack(alignment: .leading, spacing: 4) {
                    Text(label).font(.system(size: 15, weight: .semibold)).foregroundColor(.white)
                        .lineLimit(nil).fixedSize(horizontal: false, vertical: true)
                    if !m.text.isEmpty && m.state != "error" {
                        // Full text, never truncated: the bubble grows to fit it.
                        Text(m.text).font(.system(size: 13)).foregroundColor(.white.opacity(0.8))
                            .lineLimit(nil).fixedSize(horizontal: false, vertical: true)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
                .padding(.vertical, 6)
                Spacer(minLength: 0)
                if m.state != "error" && m.state != "ready" { Waveform(level: m.level, state: m.state) }
                Button(action: { closeConversation() }) {
                    Image(systemName: "xmark.circle.fill")
                        .font(.system(size: 22))
                        .foregroundColor(.white.opacity(0.55))
                }
                .buttonStyle(.plain)
                .help("Close Hermes")
            }
            .padding(.horizontal, 16).padding(.vertical, 10)
            .frame(width: HUD_WIDTH)
            .frame(minHeight: 82)
            .fixedSize(horizontal: false, vertical: true)
            .background(
                RoundedRectangle(cornerRadius: 30, style: .continuous)
                    .fill(Color(red: 0.07, green: 0.07, blue: 0.10).opacity(0.92))
                    .overlay(RoundedRectangle(cornerRadius: 30, style: .continuous)
                        .stroke(Color.white.opacity(0.12), lineWidth: 1))
            )
        }
    }
}

// MARK: - HUD window

let HUD_WIDTH: CGFloat = 600

final class HUD {
    let panel: NSPanel
    var host: NSView!
    var visible = false
    var lastText = "", lastState = ""
    init() {
        panel = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 460, height: 82),
                        styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.level = .statusBar
        panel.ignoresMouseEvents = false
        panel.becomesKeyOnlyIfNeeded = true
        panel.hidesOnDeactivate = false
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary, .ignoresCycle]
        let iconPath = Bundle.main.path(forResource: "hermes", ofType: "png")
        let icon = iconPath.flatMap { NSImage(contentsOfFile: $0) }
        let h = ClickThroughHost(rootView: HUDView(m: model, icon: icon))
        h.frame = NSRect(x: 0, y: 0, width: HUD_WIDTH, height: 82)
        host = h
        panel.contentView = h
        panel.alphaValue = 0
    }
    var screenFrame: NSRect?
    /// Size the bubble to its content (full text, no truncation), anchored bottom-centre.
    func fit() {
        guard let f = screenFrame else { return }
        host.layoutSubtreeIfNeeded()
        var size = host.fittingSize
        size.width = HUD_WIDTH
        size.height = min(max(size.height, 82), f.height * 0.85)
        let frame = NSRect(x: f.midX - size.width / 2, y: f.minY + 40, width: size.width, height: size.height)
        if panel.frame != frame { panel.setFrame(frame, display: true) }
    }
    func position() {
        let mouse = NSEvent.mouseLocation
        let screen = NSScreen.screens.first(where: { $0.frame.contains(mouse) }) ?? NSScreen.main
        screenFrame = screen?.visibleFrame
        fit()
    }
    func show() {
        guard !visible else { return }
        visible = true
        position()
        panel.orderFrontRegardless()
        NSAnimationContext.runAnimationGroup { ctx in
            ctx.duration = 0.18
            panel.animator().alphaValue = 1
        }
    }
    func hide() {
        guard visible else { return }
        visible = false
        NSAnimationContext.runAnimationGroup({ ctx in
            ctx.duration = 0.3
            panel.animator().alphaValue = 0
        }, completionHandler: { if !self.visible { self.panel.orderOut(nil) } })
    }
}

/// Accept the first click (the ✕) even though the panel never becomes key/active.
final class ClickThroughHost<V: View>: NSHostingView<V> {
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
}

var hud: HUD!

/// ✕ in the bubble: end the conversation — stop any listening/thinking/speaking and hide.
func closeConversation() {
    log("bubble closed")
    try? FileManager.default.removeItem(atPath: convPath)
    if sendWorker("close") != "ok",
       let s = try? String(contentsOfFile: pidPath, encoding: .utf8),
       let pid = Int32(s.trimmingCharacters(in: .whitespacesAndNewlines)), pid > 1 {
        kill(-pid, SIGTERM)   // one-shot ask.sh fallback run
    }
    try? FileManager.default.removeItem(atPath: statePath)
    model.state = "idle"; model.text = ""; model.level = 0
    hud.hide()
}

/// Open the voice conversation's Hermes session in a new Terminal window (no Apple Events needed:
/// a .command file opened via Launch Services runs in Terminal).
func currentSessionId() -> String {
    if let d = FileManager.default.contents(atPath: base + "/session.json"),
       let j = try? JSONSerialization.jsonObject(with: d) as? [String: Any] {
        return j["id"] as? String ?? ""
    }
    return ""
}

func openSessionInTerminal(_ sessionId: String? = nil) {
    let sid = sessionId ?? currentSessionId()
    let hermes = (configValue("hermes_bin") as? String).flatMap { $0.isEmpty ? nil : $0 } ?? "hermes"
    let cmd = sid.isEmpty ? "cd ~ && exec \(hermes)" : "cd ~ && exec \(hermes) chat --resume '\(sid)'"
    let file = base + "/continue.command"
    let body = "#!/bin/zsh -l\nclear\n\(cmd)\n"
    do {
        try body.write(toFile: file, atomically: true, encoding: .utf8)
        chmod(file, 0o755)
        let term = URL(fileURLWithPath: "/System/Applications/Utilities/Terminal.app")
        NSWorkspace.shared.open([URL(fileURLWithPath: file)], withApplicationAt: term,
                                configuration: NSWorkspace.OpenConfiguration())
        log("opened session \(sid.isEmpty ? "(new)" : sid) in Terminal")
    } catch { log("open session failed: \(error)") }
}

// MARK: - Notifications (click → continue the chat in Terminal)

final class NotifDelegate: NSObject, UNUserNotificationCenterDelegate {
    func userNotificationCenter(_ c: UNUserNotificationCenter, willPresent n: UNNotification,
                                withCompletionHandler done: @escaping (UNNotificationPresentationOptions) -> Void) {
        done([.banner, .list])
    }
    func userNotificationCenter(_ c: UNUserNotificationCenter, didReceive r: UNNotificationResponse,
                                withCompletionHandler done: @escaping () -> Void) {
        let sid = r.notification.request.content.userInfo["sid"] as? String
        log("notification clicked (sid=\(sid ?? "-"))")
        DispatchQueue.main.async { openSessionInTerminal(sid) }
        done()
    }
}
let notifDelegate = NotifDelegate()
var lastNotifiedTs: Double = 0

func postAnswerNotification(_ answer: String, sid: String? = nil) {
    let content = UNMutableNotificationContent()
    content.title = "Hermes"
    content.body = answer
    content.subtitle = "Click to continue in Terminal"
    content.userInfo = ["sid": (sid?.isEmpty == false ? sid! : currentSessionId())]
    let req = UNNotificationRequest(identifier: "hermes-answer-\(UUID().uuidString)", content: content, trigger: nil)
    UNUserNotificationCenter.current().add(req) { err in
        log(err == nil ? "notification posted" : "notification failed: \(err!)")
    }
}

func askAlive() -> Bool {
    guard let s = try? String(contentsOfFile: pidPath, encoding: .utf8),
          let pid = Int32(s.trimmingCharacters(in: .whitespacesAndNewlines)) else { return false }
    return kill(pid, 0) == 0 || kill(-pid, 0) == 0
}

func pollState() {
    var st = "idle", lvl = 0.0, txt = "", ts = 0.0
    if let d = FileManager.default.contents(atPath: statePath),
       let j = try? JSONSerialization.jsonObject(with: d) as? [String: Any] {
        st = j["state"] as? String ?? "idle"
        lvl = (j["level"] as? Double) ?? 0
        txt = j["text"] as? String ?? ""
        ts = (j["ts"] as? Double) ?? 0
    }
    // "starting" is shown locally on the key press until ask.py reports in.
    if model.state == "starting" && st == "idle" {
        if Date().timeIntervalSince(model.lastChange) > 8 { model.state = "idle"; hud.hide() }
        return
    }
    _ = ts
    // New answer from ask.py → clickable notification (click → continue in Terminal).
    if let d = FileManager.default.contents(atPath: notifyPath),
       let j = try? JSONSerialization.jsonObject(with: d) as? [String: Any],
       let nts = j["ts"] as? Double, nts != lastNotifiedTs {
        let first = lastNotifiedTs == 0
        lastNotifiedTs = nts
        // Skip a stale file left from before launch; post only fresh answers.
        if !(first && Date().timeIntervalSince1970 - nts > 30) {
            postAnswerNotification(j["text"] as? String ?? "", sid: j["sid"] as? String)
        }
    }
    let open = FileManager.default.fileExists(atPath: convPath)
    if st == "done" { st = "idle" }
    if st == "idle" && open { st = "ready" }            // bubble stays until ✕
    if st != "idle" && st != "error" && st != "ready" && !askAlive() && Date().timeIntervalSince(model.lastChange) > 3 {
        st = open ? "ready" : "idle"   // ask.py died without cleaning up
    }
    if st != model.state { model.state = st; model.lastChange = Date() }
    model.level = model.level * 0.5 + lvl * 0.5
    if txt != model.text || st != hud.lastState {
        model.text = txt; hud.lastState = st
        DispatchQueue.main.async { hud.fit() }   // after SwiftUI re-lays out the new text
    }
    if st == "idle" { hud.hide() } else { hud.show() }
}

// MARK: - fn long-press

var fnDown = false
var otherKey = false
var pending: DispatchWorkItem?
var tapRef: CFMachPort?

// MARK: - persistent voice worker (worker.py) — warm STT + Hermes API; respawned if it dies

let workerSock = base + "/worker.sock"

var workerProc: Process?
var lastWorkerSpawn = Date.distantPast

/// Send one command to the worker over its Unix socket; returns the reply or nil.
func sendWorker(_ cmd: String, timeout: Int = 2) -> String? {
    let fd = socket(AF_UNIX, SOCK_STREAM, 0)
    if fd < 0 { return nil }
    defer { close(fd) }
    var tv = timeval(tv_sec: timeout, tv_usec: 0)
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, socklen_t(MemoryLayout<timeval>.size))
    setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, socklen_t(MemoryLayout<timeval>.size))
    var addr = sockaddr_un()
    addr.sun_family = sa_family_t(AF_UNIX)
    let pathBytes = Array(workerSock.utf8)
    if pathBytes.count >= MemoryLayout.size(ofValue: addr.sun_path) { return nil }
    withUnsafeMutableBytes(of: &addr.sun_path) { buf in
        for (i, b) in pathBytes.enumerated() { buf[i] = b }
        buf[pathBytes.count] = 0
    }
    let ok = withUnsafePointer(to: &addr) {
        $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
            connect(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size))
        }
    }
    if ok != 0 { return nil }
    let msg = Array((cmd + "\n").utf8)
    if write(fd, msg, msg.count) != msg.count { return nil }
    var buf = [UInt8](repeating: 0, count: 256)
    let n = read(fd, &buf, buf.count)
    if n <= 0 { return nil }
    return String(decoding: buf[0..<n], as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
}

func ensureWorker() {
    if let p = workerProc, p.isRunning { return }
    if sendWorker("ping", timeout: 1) == "pong" { return }   // one started elsewhere
    if Date().timeIntervalSince(lastWorkerSpawn) < 10 { return } // backoff while it boots
    lastWorkerSpawn = Date()
    let p = Process()
    p.executableURL = URL(fileURLWithPath: "/bin/bash")
    p.arguments = [launcher, "worker"]
    do { try p.run(); workerProc = p; log("worker spawned (pid \(p.processIdentifier))") }
    catch { log("worker spawn failed: \(error)") }
}

func trigger() {
    log("long-press → ask")
    FileManager.default.createFile(atPath: convPath, contents: nil)
    if model.state == "idle" || model.state == "ready" || model.state == "error" {
        try? FileManager.default.removeItem(atPath: statePath)
        model.state = "starting"; model.text = ""; model.level = 0; model.lastChange = Date()
        hud.show()
    }
    if sendWorker("toggle") == "ok" { return }
    log("worker unavailable — using one-shot ask")
    ensureWorker()
    let p = Process()
    p.executableURL = URL(fileURLWithPath: "/bin/bash")
    p.arguments = [launcher, "ask"]
    do { try p.run() } catch { log("launch failed: \(error)") }
}

let callback: CGEventTapCallBack = { _, type, event, _ in
    if type == .tapDisabledByTimeout || type == .tapDisabledByUserInput {
        if let t = tapRef { CGEvent.tapEnable(tap: t, enable: true) }
        return Unmanaged.passUnretained(event)
    }
    if type == .keyDown {
        if fnDown { otherKey = true; pending?.cancel() }
        return Unmanaged.passUnretained(event)
    }
    if type == .flagsChanged {
        let isFn = event.flags.contains(.maskSecondaryFn)
        let others: CGEventFlags = [.maskCommand, .maskShift, .maskControl, .maskAlternate]
        let keycode = event.getIntegerValueField(.keyboardEventKeycode)
        if isFn && !fnDown && keycode == 63 {
            fnDown = true; otherKey = false; log("fn down")
            let w = DispatchWorkItem { if fnDown && !otherKey { trigger() } }
            pending = w
            DispatchQueue.main.asyncAfter(deadline: .now() + HOLD, execute: w)
        } else if !isFn && fnDown {
            fnDown = false; pending?.cancel()
        } else if fnDown && !event.flags.intersection(others).isEmpty {
            otherKey = true; pending?.cancel()
        }
    }
    return Unmanaged.passUnretained(event)
}

// MARK: - main

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
hud = HUD()

if CommandLine.arguments.contains("--demo") {
    // Visual test: cycle through the states without the mic; optional screenshot via HUD_SHOT.
    let seq: [(String, Double)] = [("starting", 0), ("listening", 0.2), ("listening", 0.8),
                                   ("thinking", 0), ("speaking", 0), ("idle", 0)]
    let shotDir = ProcessInfo.processInfo.environment["HUD_SHOT"]
    for (i, s) in seq.enumerated() {
        DispatchQueue.main.asyncAfter(deadline: .now() + Double(i) * 1.5) {
            model.state = s.0; model.level = s.1
            model.text = s.0 == "thinking" ? "what's the weather tomorrow in Casablanca?"
                : s.0 == "speaking" ? "Tomorrow in Casablanca it'll be mostly sunny with a high of 22 degrees and a low of 14, with a light breeze off the ocean in the afternoon. There's no rain expected, so it's a good day to be outside — maybe take a light jacket for the evening, when it cools down quickly near the coast." : ""
            if s.0 == "idle" { hud.hide() } else { hud.show() }
            DispatchQueue.main.async { hud.fit() }
            if let dir = shotDir, s.0 != "idle" {
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.7) {
                    let id = CGWindowID(hud.panel.windowNumber)
                    if let img = CGWindowListCreateImage(.null, .optionIncludingWindow, id,
                                                         [.boundsIgnoreFraming, .bestResolution]) {
                        let rep = NSBitmapImageRep(cgImage: img)
                        try? rep.representation(using: .png, properties: [:])?
                            .write(to: URL(fileURLWithPath: "\(dir)/hud_\(i)_\(s.0).png"))
                    }
                }
            }
        }
    }
    DispatchQueue.main.asyncAfter(deadline: .now() + Double(seq.count) * 1.5 + 0.5) { exit(0) }
    app.run()
}

try? FileManager.default.removeItem(atPath: convPath)   // never reopen a stale bubble at launch
UNUserNotificationCenter.current().delegate = notifDelegate
UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { ok, err in
    log("notification permission = \(ok) \(err.map { "\($0)" } ?? "")")
}
if let i = CommandLine.arguments.firstIndex(of: "--notify-test") {
    let msg = CommandLine.arguments.count > i + 1 ? CommandLine.arguments[i + 1] : "Test from Hermes"
    DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { postAnswerNotification(msg) }
}
log("preflight listen access = \(CGPreflightListenEventAccess())")
if !CGPreflightListenEventAccess() {
    log("requesting Input Monitoring access")
    CGRequestListenEventAccess()
}
let mask = (1 << CGEventType.flagsChanged.rawValue) | (1 << CGEventType.keyDown.rawValue)
if let tap = CGEvent.tapCreate(tap: .cgSessionEventTap, place: .headInsertEventTap,
                               options: .listenOnly, eventsOfInterest: CGEventMask(mask),
                               callback: callback, userInfo: nil) {
    tapRef = tap
    let src = CFMachPortCreateRunLoopSource(kCFAllocatorDefault, tap, 0)
    CFRunLoopAddSource(CFRunLoopGetMain(), src, .commonModes)
    CGEvent.tapEnable(tap: tap, enable: true)
    log("listening for long-press of 🌐/fn")
} else {
    log("could not create event tap — grant Input Monitoring to HermesKey")
}
Timer.scheduledTimer(withTimeInterval: 1.0 / 30.0, repeats: true) { _ in pollState() }
ensureWorker()
Timer.scheduledTimer(withTimeInterval: 5.0, repeats: true) { _ in
    DispatchQueue.global().async { DispatchQueue.main.async { ensureWorker() } }
}
NotificationCenter.default.addObserver(forName: NSApplication.willTerminateNotification, object: nil, queue: nil) { _ in
    workerProc?.terminate()
}
app.run()
