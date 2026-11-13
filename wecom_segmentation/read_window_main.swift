import Foundation

@main
struct WindowReader {
    static func main() {
        do {
            guard let title = readLine(), !title.isEmpty else { throw ReadError("请提供要读取的测试会话标题") }
            let data = try JSONEncoder().encode(readWeComWindow(expectedTitle: title))
            print(String(decoding: data, as: UTF8.self))
        } catch {
            fputs("\(error.localizedDescription)\n", stderr)
            exit(1)
        }
    }
}
