import Foundation

// Long-lived SSE client with exponential-backoff reconnect. The session's
// delegate queue is the main queue, so every callback, and every piece of
// state here, lives on the main thread; the traffic is a few bytes a second.
final class StreamClient: NSObject, URLSessionDataDelegate {
    var onEvent: (([String: Any]) -> Void)?
    var onConnectionChange: ((Bool) -> Void)?

    private let url: URL
    private var session: URLSession!
    private var task: URLSessionDataTask?
    private var buffer = Data()
    private var dataLines: [String] = []
    private var gotFirstByte = false
    private var delay: TimeInterval = 1
    private var stopped = false
    private var reconnect: DispatchWorkItem?

    private static let maxDelay: TimeInterval = 30
    private static let maxBuffer = 1 << 20

    init(url: URL) {
        self.url = url
        super.init()
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 3600
        config.timeoutIntervalForResource = 7 * 24 * 3600
        config.requestCachePolicy = .reloadIgnoringLocalCacheData
        config.urlCache = nil
        session = URLSession(configuration: config, delegate: self, delegateQueue: .main)
    }

    func start() {
        guard !stopped else { return }
        reconnect = nil
        buffer.removeAll()
        dataLines.removeAll()
        gotFirstByte = false
        var req = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 3600)
        req.setValue("text/event-stream", forHTTPHeaderField: "Accept")
        req.setValue("no-cache", forHTTPHeaderField: "Cache-Control")
        let t = session.dataTask(with: req)
        task = t
        t.resume()
    }

    func stop() {
        stopped = true
        reconnect?.cancel()
        reconnect = nil
        task?.cancel()
        task = nil
        session.invalidateAndCancel()
    }

    // MARK: URLSessionDataDelegate

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive response: URLResponse,
                    completionHandler: @escaping (URLSession.ResponseDisposition) -> Void) {
        let ok = (response as? HTTPURLResponse).map { $0.statusCode == 200 } ?? false
        completionHandler(ok ? .allow : .cancel)
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        guard dataTask === task else { return }
        if !gotFirstByte {
            gotFirstByte = true
            delay = 1
            onConnectionChange?(true)
        }
        buffer.append(data)
        while let nl = buffer.firstIndex(of: 0x0A) {
            var line = String(decoding: buffer[buffer.startIndex..<nl], as: UTF8.self)
            buffer.removeSubrange(buffer.startIndex...nl)
            if line.hasSuffix("\r") { line.removeLast() }
            handleLine(line)
        }
        if buffer.count > Self.maxBuffer { buffer.removeAll() }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        guard task === self.task else { return }
        self.task = nil
        onConnectionChange?(false)
        scheduleReconnect()
    }

    // MARK: SSE parsing

    private func handleLine(_ line: String) {
        if line.isEmpty {
            dispatchEvent()
            return
        }
        if line.hasPrefix(":") { return }
        let field: Substring
        var value: Substring
        if let colon = line.firstIndex(of: ":") {
            field = line[line.startIndex..<colon]
            value = line[line.index(after: colon)...]
            if value.hasPrefix(" ") { value = value.dropFirst() }
        } else {
            field = Substring(line)
            value = ""
        }
        if field == "data" { dataLines.append(String(value)) }
        // event:, id:, retry: are not used by this stream.
    }

    private func dispatchEvent() {
        guard !dataLines.isEmpty else { return }
        let payload = dataLines.joined(separator: "\n")
        dataLines.removeAll()
        guard
            let obj = try? JSONSerialization.jsonObject(with: Data(payload.utf8)),
            let dict = obj as? [String: Any]
        else { return }
        onEvent?(dict)
    }

    private func scheduleReconnect() {
        guard !stopped else { return }
        let wait = delay
        delay = min(delay * 2, Self.maxDelay)
        let work = DispatchWorkItem { [weak self] in self?.start() }
        reconnect = work
        DispatchQueue.main.asyncAfter(deadline: .now() + wait, execute: work)
    }
}
