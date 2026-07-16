import Foundation
import Vision

struct Classification: Codable {
    let label: String
    let confidence: Float
}

struct ClassificationOutput: Codable {
    let path: String
    let classifications: [Classification]
}

func fail(_ message: String, code: Int32 = 1) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(code)
}

let arguments = CommandLine.arguments
guard arguments.count >= 2 else {
    fail("usage: phototagger-classify IMAGE [MAX_RESULTS]", code: 64)
}

let path = arguments[1]
let maximum = arguments.count >= 3 ? max(1, Int(arguments[2]) ?? 20) : 20
let url = URL(fileURLWithPath: path)
guard FileManager.default.fileExists(atPath: path) else {
    fail("image does not exist: \(path)", code: 66)
}

let request = VNClassifyImageRequest()
let handler = VNImageRequestHandler(url: url, options: [:])

do {
    try handler.perform([request])
    let observations = (request.results ?? []).prefix(maximum)
    let output = ClassificationOutput(
        path: path,
        classifications: observations.map {
            Classification(label: $0.identifier, confidence: $0.confidence)
        }
    )
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.sortedKeys]
    let data = try encoder.encode(output)
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data("\n".utf8))
} catch {
    fail("classification failed for \(path): \(error)")
}

