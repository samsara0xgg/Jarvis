// swift-tools-version:5.10
import PackageDescription

let package = Package(
    name: "JarvisRemote",
    platforms: [.macOS(.v14)],
    targets: [.executableTarget(name: "JarvisRemote")]
)
