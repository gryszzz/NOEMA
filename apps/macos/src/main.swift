// TEV — native macOS window for the NOEMA trading room (read-only view).
// A single WKWebView in its own window: no tabs, no address bar, no browser chrome.
// URL: first line of ~/.tev-url, else Contents/Resources/tev-url.txt.
// Build: apps/macos/build.sh (swiftc + ad-hoc codesign, macOS only).
import Cocoa
import WebKit

let placeholderMarker = "YOUR-TAILNET"

func readFirstLine(_ path: String) -> String? {
    guard let text = try? String(contentsOfFile: path, encoding: .utf8) else { return nil }
    let line = text.split(whereSeparator: \.isNewline).first.map(String.init)?
        .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
    return line.isEmpty ? nil : line
}

func dashboardURL() -> String? {
    let home = FileManager.default.homeDirectoryForCurrentUser.path
    if let u = readFirstLine(home + "/.tev-url") { return u }
    if let p = Bundle.main.path(forResource: "tev-url", ofType: "txt") { return readFirstLine(p) }
    return nil
}

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate, NSWindowDelegate {
    var window: NSWindow!
    var webView: WKWebView!
    var home: URL!

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildMenu()
        guard let raw = dashboardURL(), !raw.contains(placeholderMarker),
              let url = URL(string: raw), url.scheme == "https" || url.scheme == "http" else {
            let a = NSAlert()
            a.messageText = "TEV needs your dashboard URL"
            a.informativeText = "Put your dashboard address (for example https://<machine>.<tailnet>.ts.net/trading-room) "
                + "on the first line of ~/.tev-url, or in TEV.app/Contents/Resources/tev-url.txt "
                + "(right-click TEV > Show Package Contents). Then open TEV again."
            a.alertStyle = .informational
            a.addButton(withTitle: "OK")
            NSApp.activate(ignoringOtherApps: true)
            a.runModal()
            NSApp.terminate(nil)
            return
        }
        home = url

        let config = WKWebViewConfiguration()
        config.websiteDataStore = .default()  // keep session/cookies between launches
        webView = WKWebView(frame: .zero, configuration: config)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.allowsBackForwardNavigationGestures = false
        webView.allowsMagnification = true

        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1600, height: 1000),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
                          backing: .buffered, defer: false)
        window.title = "TEV"
        window.titlebarAppearsTransparent = true
        window.backgroundColor = NSColor(calibratedRed: 0.04, green: 0.05, blue: 0.07, alpha: 1)
        window.minSize = NSSize(width: 900, height: 600)
        window.contentView = webView
        window.delegate = self
        window.setFrameAutosaveName("TEVMainWindow")
        if !window.setFrameUsingName("TEVMainWindow") { window.center() }
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        webView.load(URLRequest(url: url))
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    // Dashboard uses HTTP Basic auth: ask once, keep it in the login keychain.
    func webView(_ webView: WKWebView, didReceive challenge: URLAuthenticationChallenge,
                 completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
        let space = challenge.protectionSpace
        guard space.authenticationMethod == NSURLAuthenticationMethodHTTPBasic
                || space.authenticationMethod == NSURLAuthenticationMethodDefault else {
            completionHandler(.performDefaultHandling, nil); return
        }
        if challenge.previousFailureCount == 0,
           let saved = URLCredentialStorage.shared.defaultCredential(for: space) {
            completionHandler(.useCredential, saved); return
        }
        let a = NSAlert()
        a.messageText = "Log in to TEV"
        a.informativeText = challenge.previousFailureCount > 0
            ? "That didn't work. Enter your dashboard user and password."
            : "Enter your dashboard user and password."
        let user = NSTextField(frame: NSRect(x: 0, y: 30, width: 260, height: 24))
        user.placeholderString = "user"
        user.stringValue = challenge.proposedCredential?.user ?? ""
        let pass = NSSecureTextField(frame: NSRect(x: 0, y: 0, width: 260, height: 24))
        pass.placeholderString = "password"
        let box = NSView(frame: NSRect(x: 0, y: 0, width: 260, height: 54))
        box.addSubview(user); box.addSubview(pass)
        a.accessoryView = box
        a.addButton(withTitle: "Log In")
        a.addButton(withTitle: "Cancel")
        a.window.initialFirstResponder = user
        guard a.runModal() == .alertFirstButtonReturn else {
            completionHandler(.cancelAuthenticationChallenge, nil); return
        }
        let cred = URLCredential(user: user.stringValue, password: pass.stringValue, persistence: .permanent)
        URLCredentialStorage.shared.setDefaultCredential(cred, for: space)
        completionHandler(.useCredential, cred)
    }

    // Links that leave the dashboard host open in the default browser; the window stays on TEV.
    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if let u = action.request.url, action.navigationType == .linkActivated, u.host != home.host {
            NSWorkspace.shared.open(u); decisionHandler(.cancel); return
        }
        decisionHandler(.allow)
    }

    // target=_blank / window.open: same rule, never spawn extra windows.
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let u = action.request.url {
            if u.host == home.host { webView.load(action.request) } else { NSWorkspace.shared.open(u) }
        }
        return nil
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) { showError(error) }
    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        showError(error)
    }

    func showError(_ error: Error) {
        let e = error as NSError
        if e.domain == NSURLErrorDomain && e.code == NSURLErrorCancelled { return }
        let msg = (e.localizedDescription + " — is Tailscale connected? Press ⌘R to retry.")
            .replacingOccurrences(of: "&", with: "&amp;").replacingOccurrences(of: "<", with: "&lt;")
        webView.loadHTMLString("""
            <html><body style="background:#0a0d12;color:#cfd6e4;font:15px -apple-system;display:flex;\
            align-items:center;justify-content:center;height:100vh;margin:0"><div>\(msg)</div></body></html>
            """, baseURL: nil)
    }

    @objc func reloadHome(_ sender: Any?) { webView?.load(URLRequest(url: home)) }

    func buildMenu() {
        let main = NSMenu()
        let appItem = NSMenuItem(); main.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "Hide TEV", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Quit TEV", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
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
        view.addItem(withTitle: "Reload", action: #selector(reloadHome(_:)), keyEquivalent: "r")
        view.addItem(withTitle: "Enter Full Screen", action: #selector(NSWindow.toggleFullScreen(_:)), keyEquivalent: "f")
        view.items.last?.keyEquivalentModifierMask = [.command, .control]
        viewItem.submenu = view

        let winItem = NSMenuItem(); main.addItem(winItem)
        let win = NSMenu(title: "Window")
        win.addItem(withTitle: "Minimize", action: #selector(NSWindow.miniaturize(_:)), keyEquivalent: "m")
        win.addItem(withTitle: "Close", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        winItem.submenu = win
        NSApp.windowsMenu = win
        NSApp.mainMenu = main
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
