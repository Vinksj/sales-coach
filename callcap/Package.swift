// swift-tools-version: 5.9
// callcap: dual-channel call capture. System audio comes from AudioTeeCore
// (vendored, MIT, see vendor/LICENSE-audiotee.md); the microphone channel,
// TCC disclaim and frame protocol are ours.
import Foundation
import PackageDescription

let infoPlist = URL(fileURLWithPath: #filePath).deletingLastPathComponent().appendingPathComponent("Info.plist").path

let package = Package(
  name: "callcap",
  platforms: [.macOS("14.4")],
  targets: [
    .target(name: "AudioTeeCore", path: "vendor/AudioTeeCore"),
    .executableTarget(
      name: "callcap",
      dependencies: ["AudioTeeCore"],
      path: "Sources/callcap",
      linkerSettings: [
        // Embed Info.plist so TCC shows callcap's own usage descriptions.
        .unsafeFlags(["-Xlinker", "-sectcreate", "-Xlinker", "__TEXT", "-Xlinker", "__info_plist",
                      "-Xlinker", infoPlist])
      ]
    ),
  ]
)
