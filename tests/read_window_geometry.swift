import Foundation

@main
struct ReadWindowGeometryTest {
    static func main() {
        let region = CGRect(x: 100, y: 100, width: 676, height: 523)
        let centered = CGRect(x: 307, y: 150, width: 262, height: 15)
        let incoming = CGRect(x: 126, y: 150, width: 262, height: 22)
        let notice = "你已添加测试同事，现在可以开始聊天了。"
        precondition(isCenteredContactNotice(notice, in: region, at: centered))
        precondition(!isCenteredContactNotice(notice, in: region, at: incoming))
        precondition(!isCenteredContactNotice("你好，我读高二。", in: region, at: centered))
    }
}
