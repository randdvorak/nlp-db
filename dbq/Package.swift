// swift-tools-version:5.9
// dbq: English question -> PostgreSQL SELECT (fine-tuned model via llama.cpp) -> rows (libpq). The database comes from the
// project catalog embedded at build time (scripts/build-dbq.sh databases/<name>).
//
// llama.cpp is linked statically from vendor/llama (scripts/build-llama.sh, pinned tag).
// libpq comes from Homebrew (brew install libpq) and is linked dynamically.
import PackageDescription

let vendor = Context.packageDirectory + "/vendor/llama"
let staticLibs = ["llama", "ggml", "ggml-base", "ggml-cpu", "ggml-metal", "ggml-blas"]
    .map { "\(vendor)/lib/lib\($0).a" }
let libpq = "/opt/homebrew/opt/libpq"

let package = Package(
    name: "dbq",
    platforms: [.macOS(.v14)],
    targets: [
        .systemLibrary(name: "CLlama", path: "Sources/CLlama"),
        .systemLibrary(name: "CLibpq", path: "Sources/CLibpq"),
        .executableTarget(
            name: "dbq",
            dependencies: ["CLlama", "CLibpq"],
            swiftSettings: [.unsafeFlags(["-Xcc", "-I\(vendor)/include", "-Xcc", "-I\(libpq)/include"])],
            linkerSettings: [
                .unsafeFlags(staticLibs + ["-lc++", "-L\(libpq)/lib", "-lpq"]),
                .linkedFramework("Metal"),
                .linkedFramework("MetalKit"),
                .linkedFramework("Accelerate"),
                .linkedFramework("Foundation"),
            ]
        ),
    ]
)
