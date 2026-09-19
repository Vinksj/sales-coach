// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "coach-overlay",
    platforms: [.macOS(.v13)],
    products: [
        .executable(name: "coach-overlay", targets: ["coach-overlay"]),
    ],
    targets: [
        .executableTarget(name: "coach-overlay"),
    ]
)
