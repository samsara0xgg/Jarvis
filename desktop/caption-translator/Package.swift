// swift-tools-version: 6.2
import PackageDescription

let package = Package(
    name: "CaptionTranslator",
    platforms: [.macOS(.v26)],
    products: [
        .executable(name: "CaptionTranslator", targets: ["CaptionTranslator"]),
        .executable(name: "CaptionProbe", targets: ["CaptionProbe"]),
    ],
    targets: [
        .target(name: "CaptionCore"),
        .executableTarget(name: "CaptionTranslator", dependencies: ["CaptionCore"]),
        .executableTarget(name: "CaptionProbe", dependencies: ["CaptionCore"]),
        .testTarget(name: "CaptionCoreTests", dependencies: ["CaptionCore"]),
    ],
    swiftLanguageModes: [.v5]
)
