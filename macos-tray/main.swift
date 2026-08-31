// Amelia for Mac — the menu-bar companion.
//
// The desktop-specific value of Amelia is not another chat window; the phone
// and the web already have those. It is BEING the machine: starting the
// server, holding the relay line, and showing the six-digit code without
// anyone opening Terminal. This app is that, and deliberately nothing more.
//
// It shells out to bootstrap.py and connect.py rather than reimplementing
// them: those two scripts are the tested path (the connector survived a
// kill -9 under launchd during testing), and a second implementation of
// pairing would be a second thing to get wrong. The app's job is to be the
// friendly hand on the proven machinery.
//
// Build: ./build.sh   (swiftc, single file, no dependencies)
// Probe: ./AmeliaTray --probe   (headless: start server, parse a pairing
//        code from connect.py, print CODE=nnnnnn, exit — used by tests)

import SwiftUI
import AppKit

// ── Where the repo lives ────────────────────────────────────────────────────
// The tray ships inside the amelias-webui checkout, so the scripts are
// siblings. AMELIA_HOME overrides for people who put things elsewhere.
func repoRoot() -> URL? {
    let fm = FileManager.default
    if let env = ProcessInfo.processInfo.environment["AMELIA_HOME"] {
        let u = URL(fileURLWithPath: (env as NSString).expandingTildeInPath)
        if fm.fileExists(atPath: u.appendingPathComponent("bootstrap.py").path) { return u }
    }
    // Walk up from the executable: macos-tray/AmeliaTray → macos-tray → repo.
    var u = URL(fileURLWithPath: CommandLine.arguments[0]).resolvingSymlinksInPath()
    for _ in 0..<4 {
        u.deleteLastPathComponent()
        if fm.fileExists(atPath: u.appendingPathComponent("bootstrap.py").path) { return u }
    }
    let home = fm.homeDirectoryForCurrentUser.appendingPathComponent("amelias-webui")
    if fm.fileExists(atPath: home.appendingPathComponent("bootstrap.py").path) { return home }
    return nil
}

let localServer = ProcessInfo.processInfo.environment["AMELIA_LOCAL"] ?? "127.0.0.1:8787"

// ── The machinery ───────────────────────────────────────────────────────────
final class Tray: ObservableObject {
    @Published var serverUp = false
    @Published var code: String?          // shown big until the phone claims it
    @Published var paired = false         // ~/.amelia/machine.json exists
    @Published var busy = false
    @Published var lastError: String?
    private var connector: Process?
    private var timer: Timer?

    init() {
        probe()
        timer = Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in self?.probe() }
    }

    func probe() {
        paired = FileManager.default.fileExists(
            atPath: NSString("~/.amelia/machine.json").expandingTildeInPath)
        var req = URLRequest(url: URL(string: "http://\(localServer)/health")!)
        req.timeoutInterval = 3
        URLSession.shared.dataTask(with: req) { _, resp, _ in
            DispatchQueue.main.async {
                self.serverUp = (resp as? HTTPURLResponse)?.statusCode == 200
            }
        }.resume()
    }

    /// bootstrap.py starts the server detached and exits — that is its
    /// documented default path, so waiting for it is bounded.
    func startServer(root: URL) -> Bool {
        let p = Process()
        p.currentDirectoryURL = root
        p.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        p.arguments = ["python3", "bootstrap.py", "--no-browser"]
        do { try p.run() } catch { lastError = "Could not run bootstrap.py: \(error.localizedDescription)"; return false }
        p.waitUntilExit()
        return p.terminationStatus == 0
    }

    /// Runs connect.py, watching stdout for the six-digit code. The connector
    /// stays alive holding the relay line; the code is only printed on first
    /// enrolment, so an already-paired machine goes straight to "online".
    func startConnector(root: URL) {
        let p = Process()
        p.currentDirectoryURL = root
        p.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        p.arguments = ["python3", "connect.py", "--local", localServer]
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = pipe
        pipe.fileHandleForReading.readabilityHandler = { [weak self] h in
            guard let line = String(data: h.availableData, encoding: .utf8) else { return }
            if let r = line.range(of: #"code:\s+(\d{6})"#, options: .regularExpression) {
                let digits = line[r].filter { $0.isNumber }
                DispatchQueue.main.async { self?.code = String(digits) }
            }
        }
        do { try p.run(); connector = p } catch {
            lastError = "Could not run connect.py: \(error.localizedDescription)"
        }
    }

    func start() {
        guard let root = repoRoot() else {
            lastError = "Could not find the amelias-webui folder. Set AMELIA_HOME."
            return
        }
        busy = true; lastError = nil
        DispatchQueue.global().async {
            if !self.serverUp { _ = self.startServer(root: root) }
            DispatchQueue.main.async {
                self.startConnector(root: root)
                self.busy = false
                self.probe()
            }
        }
    }

    func stop() {
        connector?.terminate(); connector = nil; code = nil
    }

    /// Hands keeping-alive to launchd — the same --install the terminal flow
    /// offers, one click here.
    func installAtLogin() {
        guard let root = repoRoot() else { return }
        let p = Process()
        p.currentDirectoryURL = root
        p.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        p.arguments = ["python3", "connect.py", "--install", "--local", localServer]
        try? p.run(); p.waitUntilExit()
    }
}

// ── The face ────────────────────────────────────────────────────────────────
struct MenuBody: View {
    @ObservedObject var tray: Tray
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Circle().fill(tray.serverUp ? .green : .secondary).frame(width: 9, height: 9)
                Text(tray.serverUp ? "Amelia is running on this Mac" : "Not running")
                    .font(.system(size: 13, weight: .semibold))
            }
            if let code = tray.code {
                VStack(alignment: .leading, spacing: 4) {
                    Text("PAIRING CODE").font(.system(size: 10, weight: .bold)).foregroundColor(.secondary)
                    Text(code.map(String.init).joined(separator: " "))
                        .font(.system(size: 28, weight: .bold, design: .monospaced))
                    Text("Open Amelia's Agent on your phone →\nProfile → Connect your own computer")
                        .font(.system(size: 11)).foregroundColor(.secondary)
                }
                .padding(10)
                .background(RoundedRectangle(cornerRadius: 8).fill(Color.secondary.opacity(0.12)))
            } else if tray.paired {
                Text("Paired with your account · holding the line")
                    .font(.system(size: 12)).foregroundColor(.secondary)
            }
            if let err = tray.lastError {
                Text(err).font(.system(size: 11)).foregroundColor(.red)
            }
            Divider()
            if tray.serverUp {
                Button("Stop connector") { tray.stop() }
            } else {
                Button(tray.busy ? "Starting…" : "Start Amelia on this Mac") { tray.start() }
                    .disabled(tray.busy)
            }
            Button("Keep running after restarts") { tray.installAtLogin() }
            Button("Quit") { NSApplication.shared.terminate(nil) }
        }
        .padding(14)
        .frame(width: 280)
    }
}

@main
struct AmeliaTrayApp: App {
    @StateObject private var tray = Tray()

    init() {
        // Headless probe for tests: start nothing graphical, run the real
        // pairing pipeline against whatever AMELIA_API points at, print the
        // parsed code, leave. Proof the machinery works without a human
        // clicking a menu bar.
        if CommandLine.arguments.contains("--probe") {
            guard let root = repoRoot() else { print("PROBE-FAIL no repo"); exit(2) }
            let p = Process()
            p.currentDirectoryURL = root
            p.executableURL = URL(fileURLWithPath: "/usr/bin/env")
            p.arguments = ["python3", "connect.py", "--local", localServer]
            let pipe = Pipe(); p.standardOutput = pipe; p.standardError = pipe
            do { try p.run() } catch { print("PROBE-FAIL \(error)"); exit(2) }
            let deadline = Date().addingTimeInterval(20)
            var buf = ""
            while Date() < deadline {
                let d = pipe.fileHandleForReading.availableData
                if d.isEmpty { usleep(100_000); continue }
                buf += String(data: d, encoding: .utf8) ?? ""
                if let r = buf.range(of: #"code:\s+(\d{6})"#, options: .regularExpression) {
                    print("CODE=" + buf[r].filter { $0.isNumber })
                    p.terminate(); exit(0)
                }
            }
            print("PROBE-FAIL timeout; saw: \(buf.suffix(300))")
            p.terminate(); exit(1)
        }
    }

    var body: some Scene {
        MenuBarExtra("Amelia", systemImage: tray.serverUp ? "leaf.fill" : "leaf") {
            MenuBody(tray: tray)
        }
        .menuBarExtraStyle(.window)
    }
}
