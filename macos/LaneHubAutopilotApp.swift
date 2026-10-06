import SwiftUI
import AppKit

// MARK: - Project Model

final class Project: Identifiable, ObservableObject {
    let id = UUID()
    let dir: String
    @Published var lane: String
    @Published var base: String
    @Published var apiKey: String
    @Published var botUsername: String?
    @Published var isOn: Bool = false
    @Published var until: Date? = nil
    var process: Process? = nil

    var folderName: String {
        return (dir as NSString).lastPathComponent
    }

    var botDisplay: String {
        if let b = botUsername, !b.isEmpty {
            return b
        }
        return lane
    }

    var statusText: String {
        if isOn, let u = until {
            let f = DateFormatter()
            f.dateFormat = "HH:mm"
            return "ON until \(f.string(from: u))"
        }
        return "OFF"
    }

    init(dir: String, lane: String, base: String, apiKey: String, botUsername: String? = nil) {
        self.dir = dir
        self.lane = lane
        self.base = base
        self.apiKey = apiKey
        self.botUsername = botUsername
    }
}

// MARK: - App State & Orchestrator

final class AppModel: ObservableObject {
    static let shared = AppModel()

    @Published var projects: [Project] = []
    private var pollTimer: Timer? = nil
    private let userDefaultsKey = "LaneHubProjects"

    var isAnyOn: Bool {
        return projects.contains { $0.isOn }
    }

    private init() {
        loadSavedProjects()
    }

    // MARK: - Persistence

    func loadSavedProjects() {
        let savedDirs = UserDefaults.standard.stringArray(forKey: userDefaultsKey) ?? []
        for dir in savedDirs {
            let cleanDir = (dir as NSString).standardizingPath
            if let env = AppModel.parseEnv(at: cleanDir),
               let lane = env["LANEHUB_LANE"],
               let base = env["LANEHUB_BASE"],
               let key = env["LANEHUB_API_KEY"] {
                let p = Project(dir: cleanDir, lane: lane, base: base, apiKey: key, botUsername: env["LANEHUB_BOT_USERNAME"])
                projects.append(p)
                fetchInfo(for: p)
            }
        }
    }

    func saveProjects() {
        let dirs = projects.map { $0.dir }
        UserDefaults.standard.set(dirs, forKey: userDefaultsKey)
    }

    // MARK: - Env Parsing

    static func parseEnv(at dir: String) -> [String: String]? {
        let envPath = (dir as NSString).appendingPathComponent(".lanehub.env")
        guard let content = try? String(contentsOfFile: envPath, encoding: .utf8) else {
            return nil
        }
        var dict: [String: String] = [:]
        for line in content.components(separatedBy: .newlines) {
            var trimmed = line.trimmingCharacters(in: .whitespacesAndNewlines)
            if trimmed.isEmpty || trimmed.hasPrefix("#") { continue }
            if trimmed.hasPrefix("export ") {
                trimmed = String(trimmed.dropFirst(7)).trimmingCharacters(in: .whitespaces)
            }
            guard let eqIdx = trimmed.firstIndex(of: "=") else { continue }
            let key = String(trimmed[..<eqIdx]).trimmingCharacters(in: .whitespaces)
            var val = String(trimmed[trimmed.index(after: eqIdx)...]).trimmingCharacters(in: .whitespaces)
            if (val.hasPrefix("\"") && val.hasSuffix("\"")) || (val.hasPrefix("'") && val.hasSuffix("'")) {
                val = String(val.dropFirst().dropLast())
            }
            dict[key] = val
        }
        return dict
    }

    // MARK: - Watcher Download

    static func downloadWatcher(from base: String, completion: @escaping (Result<URL, Error>) -> Void) {
        let appSupport = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first!
        let appDir = appSupport.appendingPathComponent("LaneHub Autopilot", isDirectory: true)
        try? FileManager.default.createDirectory(at: appDir, withIntermediateDirectories: true)
        let watcherURL = appDir.appendingPathComponent("watcher.py")

        let cleanBase = base.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        guard let url = URL(string: "\(cleanBase)/watcher.py") else {
            if FileManager.default.fileExists(atPath: watcherURL.path) {
                completion(.success(watcherURL))
            } else {
                completion(.failure(NSError(domain: "LaneHub", code: -1, userInfo: [NSLocalizedDescriptionKey: "Invalid base URL: \(base)"])))
            }
            return
        }

        var req = URLRequest(url: url)
        req.timeoutInterval = 10.0

        let task = URLSession.shared.dataTask(with: req) { data, response, error in
            if let data = data, let http = response as? HTTPURLResponse, http.statusCode == 200, !data.isEmpty {
                do {
                    try data.write(to: watcherURL, options: .atomic)
                    completion(.success(watcherURL))
                    return
                } catch {
                    // Fall back to existing if write fails
                }
            }
            if FileManager.default.fileExists(atPath: watcherURL.path) {
                completion(.success(watcherURL))
            } else {
                let err = error ?? NSError(domain: "LaneHub", code: -1, userInfo: [NSLocalizedDescriptionKey: "Failed to download watcher.py and no cached copy found"])
                completion(.failure(err))
            }
        }
        task.resume()
    }

    // MARK: - Project Actions

    func start(project: Project, hours: Int = 8) {
        guard let env = AppModel.parseEnv(at: project.dir),
              let lane = env["LANEHUB_LANE"],
              let base = env["LANEHUB_BASE"],
              let key = env["LANEHUB_API_KEY"] else {
            showAlert(title: "Configuration Error", message: "Missing LANEHUB_BASE, LANEHUB_LANE, or LANEHUB_API_KEY in .lanehub.env")
            return
        }
        project.lane = lane
        project.base = base
        project.apiKey = key
        if let bot = env["LANEHUB_BOT_USERNAME"] {
            project.botUsername = bot
        }

        let cleanBase = base.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        guard let postURL = URL(string: "\(cleanBase)/\(lane)/autopilot") else { return }

        var req = URLRequest(url: postURL)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.setValue(key, forHTTPHeaderField: "X-Bridge-Token")
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["on": true, "hours": hours])

        let task = URLSession.shared.dataTask(with: req) { [weak self] data, response, error in
            guard let self = self else { return }
            if let http = response as? HTTPURLResponse, http.statusCode != 200 {
                DispatchQueue.main.async {
                    self.showAlert(title: "Autopilot Error", message: "Hub returned HTTP \(http.statusCode) when starting autopilot.")
                }
                return
            }

            AppModel.downloadWatcher(from: cleanBase) { [weak self] result in
                guard let self = self else { return }
                switch result {
                case .failure(let err):
                    DispatchQueue.main.async {
                        self.showAlert(title: "Watcher Download Error", message: err.localizedDescription)
                    }
                case .success(let watcherURL):
                    DispatchQueue.main.async {
                        self.runProcess(for: project, watcherURL: watcherURL, hours: hours)
                    }
                }
            }
        }
        task.resume()
    }

    private func runProcess(for project: Project, watcherURL: URL, hours: Int) {
        if let old = project.process, old.isRunning {
            old.terminate()
        }

        let logPath = (project.dir as NSString).appendingPathComponent(".lanehub-autopilot.log")
        if !FileManager.default.fileExists(atPath: logPath) {
            FileManager.default.createFile(atPath: logPath, contents: nil)
        }
        let logHandle = FileHandle(forWritingAtPath: logPath)
        logHandle?.seekToEndOfFile()

        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/bin/zsh")
        proc.arguments = ["-lc", "exec python3 \"\(watcherURL.path)\""]
        var env = ProcessInfo.processInfo.environment
        env["CLAUDE_PROJECT_DIR"] = project.dir
        proc.environment = env
        proc.standardOutput = logHandle
        proc.standardError = logHandle

        let dir = project.dir
        proc.terminationHandler = { [weak self] _ in
            DispatchQueue.main.async {
                self?.handleProcessExited(forDir: dir)
            }
        }

        do {
            try proc.run()
            project.process = proc
            project.isOn = true
            project.until = Date().addingTimeInterval(Double(hours) * 3600)
            updatePollingTimer()
            fetchInfo(for: project)
        } catch {
            showAlert(title: "Process Error", message: "Failed to run watcher: \(error.localizedDescription)")
        }
    }

    func stop(project: Project) {
        let cleanBase = project.base.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        if let postURL = URL(string: "\(cleanBase)/\(project.lane)/autopilot") {
            var req = URLRequest(url: postURL)
            req.httpMethod = "POST"
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.setValue(project.apiKey, forHTTPHeaderField: "X-Bridge-Token")
            req.httpBody = try? JSONSerialization.data(withJSONObject: ["on": false])
            URLSession.shared.dataTask(with: req).resume()
        }

        if let proc = project.process, proc.isRunning {
            let pid = proc.processIdentifier
            proc.terminate()
            DispatchQueue.global().asyncAfter(deadline: .now() + 5.0) {
                if proc.isRunning {
                    kill(pid, SIGKILL)
                }
            }
        }
        project.process = nil
        project.isOn = false
        project.until = nil
        updatePollingTimer()
    }

    func remove(project: Project) {
        stop(project: project)
        projects.removeAll { $0.id == project.id }
        saveProjects()
    }

    func handleProcessExited(forDir dir: String) {
        if let p = projects.first(where: { $0.dir == dir }) {
            p.isOn = false
            p.until = nil
            p.process = nil
            updatePollingTimer()
        }
    }

    // MARK: - Polling

    func updatePollingTimer() {
        let anyOn = projects.contains { $0.isOn }
        if anyOn {
            if pollTimer == nil {
                pollTimer = Timer.scheduledTimer(withTimeInterval: 15.0, repeats: true) { [weak self] _ in
                    self?.pollActiveProjects()
                }
                pollActiveProjects()
            }
        } else {
            pollTimer?.invalidate()
            pollTimer = nil
        }
    }

    func pollActiveProjects() {
        let active = projects.filter { $0.isOn }
        if active.isEmpty {
            updatePollingTimer()
            return
        }

        for p in active {
            let cleanBase = p.base.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
            guard let url = URL(string: "\(cleanBase)/\(p.lane)/autopilot") else { continue }
            var req = URLRequest(url: url)
            req.setValue(p.apiKey, forHTTPHeaderField: "X-Bridge-Token")
            req.timeoutInterval = 10.0

            URLSession.shared.dataTask(with: req) { [weak self, weak p] data, _, _ in
                guard let self = self, let p = p, let data = data else { return }
                guard let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
                let on = json["on"] as? Bool ?? false
                let untilTs = json["until"] as? Double

                DispatchQueue.main.async {
                    if !on {
                        p.isOn = false
                        p.until = nil
                        if let proc = p.process, proc.isRunning {
                            proc.terminate()
                        }
                        p.process = nil
                        self.updatePollingTimer()
                    } else if let ts = untilTs {
                        p.until = Date(timeIntervalSince1970: ts)
                    }
                }
            }.resume()
        }
    }

    func fetchInfo(for project: Project) {
        let cleanBase = project.base.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        guard let url = URL(string: "\(cleanBase)/\(project.lane)/info") else { return }
        var req = URLRequest(url: url)
        req.setValue(project.apiKey, forHTTPHeaderField: "X-Bridge-Token")
        req.timeoutInterval = 5.0
        URLSession.shared.dataTask(with: req) { [weak project] data, _, _ in
            guard let project = project, let data = data else { return }
            guard let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
            if let bot = json["botUsername"] as? String, !bot.isEmpty {
                DispatchQueue.main.async {
                    project.botUsername = bot
                }
            }
        }.resume()
    }

    // MARK: - Open Panel & URL Scheme

    func addProjectPrompt() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        panel.canCreateDirectories = false
        panel.prompt = "Add"
        panel.title = "Select Project Folder"
        NSApp.activate(ignoringOtherApps: true)
        if panel.runModal() == .OK, let url = panel.url {
            let cleanDir = (url.path as NSString).standardizingPath
            let envPath = (cleanDir as NSString).appendingPathComponent(".lanehub.env")
            if FileManager.default.fileExists(atPath: envPath) {
                addAndStart(dir: cleanDir, hours: 8)
            } else {
                let alert = NSAlert()
                alert.messageText = "Missing .lanehub.env"
                alert.informativeText = "The selected folder does not contain a .lanehub.env file.\n\nPlease set up .lanehub.env before adding the project."
                alert.alertStyle = .warning
                alert.runModal()
            }
        }
    }

    func addAndStart(dir: String, hours: Int) {
        let cleanDir = (dir as NSString).standardizingPath
        if let p = projects.first(where: { $0.dir == cleanDir }) {
            start(project: p, hours: hours)
            return
        }
        guard let env = AppModel.parseEnv(at: cleanDir),
              let lane = env["LANEHUB_LANE"],
              let base = env["LANEHUB_BASE"],
              let key = env["LANEHUB_API_KEY"] else {
            showAlert(title: "Invalid Project", message: "Folder does not contain a valid .lanehub.env: \(cleanDir)")
            return
        }
        let p = Project(dir: cleanDir, lane: lane, base: base, apiKey: key, botUsername: env["LANEHUB_BOT_USERNAME"])
        projects.append(p)
        saveProjects()
        fetchInfo(for: p)
        start(project: p, hours: hours)
    }

    func handleURL(_ url: URL) {
        guard url.scheme == "lanehub-autopilot" else { return }
        let rawAction = url.host ?? url.path.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        let action = rawAction.lowercased()
        guard let comps = URLComponents(url: url, resolvingAgainstBaseURL: false) else { return }
        guard let dir = comps.queryItems?.first(where: { $0.name == "dir" })?.value, !dir.isEmpty else { return }
        let hoursStr = comps.queryItems?.first(where: { $0.name == "hours" })?.value
        let hours = Int(hoursStr ?? "8") ?? 8

        if action == "start" {
            addAndStart(dir: dir, hours: hours)
        } else if action == "stop" {
            let cleanDir = (dir as NSString).standardizingPath
            if let p = projects.first(where: { $0.dir == cleanDir }) {
                stop(project: p)
            }
        }
    }

    func quit() {
        for p in projects {
            if p.isOn || (p.process?.isRunning ?? false) {
                stop(project: p)
            }
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) {
            NSApplication.shared.terminate(nil)
        }
    }

    func cleanupBeforeTerminate() {
        for p in projects {
            if let proc = p.process, proc.isRunning {
                kill(proc.processIdentifier, SIGKILL)
            }
        }
    }

    private func showAlert(title: String, message: String) {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = message
        alert.alertStyle = .warning
        alert.runModal()
    }
}

// MARK: - App Delegate

final class AppDelegate: NSObject, NSApplicationDelegate {
    func application(_ application: NSApplication, open urls: [URL]) {
        for url in urls {
            AppModel.shared.handleURL(url)
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        AppModel.shared.cleanupBeforeTerminate()
    }
}

// MARK: - SwiftUI Views

struct MenuView: View {
    @ObservedObject var model: AppModel

    var body: some View {
        if model.projects.isEmpty {
            Text("No projects")
            Divider()
        } else {
            ForEach(model.projects) { p in
                Text("@\(p.botDisplay) · \(p.folderName)")
                Text("Autopilot: \(p.statusText)")
                Button("Start (8 h)") {
                    model.start(project: p, hours: 8)
                }
                Button("Stop") {
                    model.stop(project: p)
                }
                .disabled(!p.isOn)
                Button("Remove project") {
                    model.remove(project: p)
                }
                Divider()
            }
        }

        Button("Add project…") {
            model.addProjectPrompt()
        }
        Divider()
        Button("Quit LaneHub Autopilot") {
            model.quit()
        }
    }
}

@main
struct LaneHubAutopilotApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var appDelegate
    @StateObject private var model = AppModel.shared

    var body: some Scene {
        MenuBarExtra {
            MenuView(model: model)
                .onOpenURL { url in
                    model.handleURL(url)
                }
        } label: {
            Image(systemName: model.isAnyOn ? "paperplane.fill" : "paperplane")
        }
    }
}
