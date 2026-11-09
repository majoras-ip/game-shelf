import Cocoa
import WebKit

/// Invisible strip along the top of the window that lets you drag it around (double-click to zoom).
final class DragStrip: NSView {
    override var mouseDownCanMoveWindow: Bool { true }
    override func mouseDown(with event: NSEvent) {
        if event.clickCount == 2 { window?.zoom(nil); return }
        window?.performDrag(with: event)
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate, WKNavigationDelegate {
    var window: NSWindow!
    var web: WKWebView!
    var server: Process?
    var loaded = false
    let shelfDir: String

    init(dir: String) { shelfDir = dir; super.init() }

    func applicationDidFinishLaunching(_ note: Notification) {
        buildMenu()
        let cfg = WKWebViewConfiguration()
        cfg.mediaTypesRequiringUserActionForPlayback = []
        let frame = NSRect(x: 0, y: 0, width: 1280, height: 820)
        web = WKWebView(frame: frame, configuration: cfg)
        web.navigationDelegate = self
        window = NSWindow(contentRect: frame,
                          styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
                          backing: .buffered, defer: false)
        window.title = "Game Shelf"
        window.titlebarAppearsTransparent = true
        window.titleVisibility = .hidden
        window.backgroundColor = NSColor(red: 0.08, green: 0.07, blue: 0.18, alpha: 1)
        window.minSize = NSSize(width: 760, height: 520)
        let container = NSView(frame: frame)
        web.frame = container.bounds
        web.autoresizingMask = [.width, .height]
        container.addSubview(web)
        let strip = DragStrip(frame: NSRect(x: 0, y: frame.height - 26, width: frame.width, height: 26))
        strip.autoresizingMask = [.width, .minYMargin]
        container.addSubview(strip)
        window.contentView = container
        window.isMovableByWindowBackground = true
        window.center()
        window.setFrameAutosaveName("GameShelfWindow")
        window.delegate = self
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        showMessage("Loading your games…")
        startServer()
        NSWorkspace.shared.notificationCenter.addObserver(
            self, selector: #selector(otherAppQuit(_:)),
            name: NSWorkspace.didTerminateApplicationNotification, object: nil)
    }

    func startServer() {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        p.arguments = ["python3", "-u", "launcher.py", "--no-open", "--no-idle"]
        p.currentDirectoryURL = URL(fileURLWithPath: shelfDir)
        var env = ProcessInfo.processInfo.environment
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        p.environment = env
        let out = Pipe(), err = Pipe()
        p.standardOutput = out
        p.standardError = err
        out.fileHandleForReading.readabilityHandler = { [weak self] h in
            let data = h.availableData
            if data.isEmpty { h.readabilityHandler = nil; return }
            guard let text = String(data: data, encoding: .utf8),
                  let r = text.range(of: "http://127\\.0\\.0\\.1:[0-9]+/", options: .regularExpression) else { return }
            let url = String(text[r])
            DispatchQueue.main.async { self?.loadShelf(url) }
        }
        p.terminationHandler = { [weak self] _ in
            let msg = String(data: err.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
            DispatchQueue.main.async {
                guard let self = self, !self.loaded else { return }
                self.showMessage("Game Shelf couldn't start.", detail: msg)
            }
        }
        do { try p.run(); server = p } catch {
            showMessage("Game Shelf couldn't start.", detail: "Python 3 wasn't found. Open Terminal and run: xcode-select --install")
        }
    }

    func loadShelf(_ url: String) {
        guard !loaded, let u = URL(string: url) else { return }
        loaded = true
        web.load(URLRequest(url: u))
    }

    func showMessage(_ title: String, detail: String = "") {
        let esc = detail.replacingOccurrences(of: "&", with: "&amp;").replacingOccurrences(of: "<", with: "&lt;")
        web.loadHTMLString("""
        <html><body style="margin:0;height:100vh;display:grid;place-items:center;background:#15122f;color:#f4f1ff;font:600 22px -apple-system,system-ui">
        <div style="text-align:center;max-width:640px;padding:24px">\(title)
        <pre style="font:13px ui-monospace,monospace;color:#aaa4d8;white-space:pre-wrap;margin-top:16px">\(esc)</pre></div></body></html>
        """, baseURL: nil)
    }

    @objc func otherAppQuit(_ note: Notification) {
        guard let app = note.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication else { return }
        let emulators = ["dolphin", "retroarch", "duckstation", "pcsx2", "ppsspp", "ppssppsdl", "xemu", "cemu"]
        let name = (app.localizedName ?? app.executableURL?.lastPathComponent ?? "").lowercased()
        guard app.bundleIdentifier == "org.dolphin-emu.dolphin" || emulators.contains(where: { name.hasPrefix($0) }) else { return }
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    @objc func reloadShelf(_ sender: Any?) { web.reload() }

    // The page only lets videos play after a click, so give it one quiet click when it loads.
    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        guard loaded else { return }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) { [weak self] in self?.primeMedia() }
    }

    func primeMedia() {
        guard let win = window else { return }
        let y: CGFloat = web.isFlipped ? 6 : web.bounds.maxY - 6
        let loc = web.convert(NSPoint(x: web.bounds.midX, y: y), to: nil)
        for type in [NSEvent.EventType.leftMouseDown, .leftMouseUp] {
            guard let e = NSEvent.mouseEvent(with: type, location: loc, modifierFlags: [],
                                             timestamp: ProcessInfo.processInfo.systemUptime,
                                             windowNumber: win.windowNumber, context: nil,
                                             eventNumber: 0, clickCount: 1, pressure: 1) else { continue }
            if type == .leftMouseDown { web.mouseDown(with: e) } else { web.mouseUp(with: e) }
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func applicationWillTerminate(_ note: Notification) {
        if let s = server, s.isRunning { s.terminate() }
    }

    func buildMenu() {
        let main = NSMenu()
        let appItem = NSMenuItem(); main.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "Hide Game Shelf", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Quit Game Shelf", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu

        let editItem = NSMenuItem(); main.addItem(editItem)
        let edit = NSMenu(title: "Edit")
        edit.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = edit

        let viewItem = NSMenuItem(); main.addItem(viewItem)
        let view = NSMenu(title: "View")
        let reload = NSMenuItem(title: "Refresh Games", action: #selector(reloadShelf(_:)), keyEquivalent: "r")
        reload.target = self
        view.addItem(reload)
        let fs = NSMenuItem(title: "Enter Full Screen", action: #selector(NSWindow.toggleFullScreen(_:)), keyEquivalent: "f")
        fs.keyEquivalentModifierMask = [.control, .command]
        view.addItem(fs)
        viewItem.submenu = view

        let winItem = NSMenuItem(); main.addItem(winItem)
        let win = NSMenu(title: "Window")
        win.addItem(withTitle: "Minimize", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        win.addItem(withTitle: "Close", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        winItem.submenu = win
        NSApp.mainMenu = main
    }
}

let dir = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : FileManager.default.currentDirectoryPath
let app = NSApplication.shared
let delegate = AppDelegate(dir: dir)
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
