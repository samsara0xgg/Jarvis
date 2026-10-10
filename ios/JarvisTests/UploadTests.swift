import XCTest
import UIKit
@testable import Jarvis

/// The limits and shapes of what the phone sends up with a turn (docs/spec.html `POST /inherent/attachments`, ADR 0211).
final class UploadTests: XCTestCase {
    func testAPictureIsDecodedAtAtMost1568Wide() {
        XCTAssertEqual(AttachUpload.maxPixel(width: 3000, height: 2000), 1568, "landscape: the wide side is the one that shrinks")
        XCTAssertEqual(AttachUpload.maxPixel(width: 3024, height: 4032), 2091, "portrait: 1568 wide makes the long side 2091")
        XCTAssertEqual(AttachUpload.maxPixel(width: 1200, height: 800), 1200, "already narrow enough: left alone")
        XCTAssertEqual(AttachUpload.maxPixel(width: 800, height: 2400), 2400, "a tall narrow one is not shrunk for being tall")
    }

    func testAPictureFileGoesUpAsAShrunkJPEGUnderTheLimit() throws {
        let f = UIGraphicsImageRendererFormat(); f.scale = 1
        let png = UIGraphicsImageRenderer(size: CGSize(width: 3000, height: 2000), format: f).image { c in
            UIColor.red.setFill(); c.fill(CGRect(x: 0, y: 0, width: 3000, height: 2000))
        }.pngData()!
        let url = AttachTemp.newFile("大图.png")
        try png.write(to: url)
        defer { AttachTemp.discard(url) }
        let shrunk = try XCTUnwrap(AttachUpload.shrunk(url: url))
        XCTAssertEqual(shrunk.size.width * shrunk.scale, 1568, accuracy: 1)
        XCTAssertEqual(shrunk.size.height * shrunk.scale, 1045, accuracy: 1)
        let jpeg = try XCTUnwrap(AttachUpload.jpeg(shrunk))
        XCTAssertEqual(Array(jpeg.prefix(3)), [0xFF, 0xD8, 0xFF], "JPEG bytes: the brain reads the kind from them")
        XCTAssertLessThanOrEqual(jpeg.count, AttachUpload.maxImageBytes)
        XCTAssertEqual(AttachUpload.jpegName("大图.png"), "大图.jpg")
        XCTAssertEqual(AttachUpload.jpegName("照片"), "照片.jpg")
    }

    func testATurnHoldsAtMostFourFilesAndEightMiBOfPictures() {
        XCTAssertNil(AttachUpload.check(count: 4, imageBytes: 8 * 1024 * 1024))
        XCTAssertEqual(AttachUpload.check(count: 5), "一次最多发 4 个")
        XCTAssertEqual(AttachUpload.check(count: 2, imageBytes: 8 * 1024 * 1024 + 1), "图片加起来超过 8 MB")
    }

    func testTheBrainsRefusalsAreToldInChineseWithTheFilesName() {
        XCTAssertEqual(AttachUpload.refusal(status: 415, name: "简历.pdf"), "「简历.pdf」是 PDF，她读不了")
        XCTAssertEqual(AttachUpload.refusal(status: 415, name: "a.zip"), "「a.zip」她读不了，只收图片和文字")
        XCTAssertEqual(AttachUpload.refusal(status: 413, name: "一个名字非常非常长的文件.md"), "「一个名字非常非常长…」太大了，发小一点的")
        XCTAssertEqual(AttachUpload.refusal(status: 422, name: "x.jpg"), "她现在读不了图片")
        XCTAssertEqual(AttachUpload.refusal(status: 507, name: "x"), "大脑存文件的地方满了")
    }

    func testTheFormIsOneFileFieldWithItsBoundary() throws {
        let part = AttachUpload.Part(name: "笔\"记\r\n.md", mime: "application/octet-stream", data: Data("你好".utf8), isImage: false)
        let body = String(decoding: AttachUpload.multipart(part, boundary: "BND"), as: UTF8.self)
        XCTAssertEqual(body, "--BND\r\nContent-Disposition: form-data; name=\"file\"; filename=\"笔_记__.md\"\r\nContent-Type: application/octet-stream\r\n\r\n你好\r\n--BND--\r\n",
                       "the name cannot break out of its header")
    }
}
