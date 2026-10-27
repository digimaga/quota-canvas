// swift-tools-version: 6.0
import PackageDescription

// QuotaCanvas menu-bar app. Builds with the Command Line Tools only
// (swift build -c release; see scripts/build.sh for the .app assembly).

let package = Package(
    name: "QuotaCanvasMenuBar",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(
            name: "QuotaCanvasApp",
            linkerSettings: [
                .linkedFramework("AppKit"),
                .linkedFramework("ServiceManagement"),
                .linkedFramework("UserNotifications"),
            ]
        ),
    ],
    swiftLanguageModes: [.v5]
)
