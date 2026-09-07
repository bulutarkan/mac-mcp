import Foundation
import NaturalLanguage

let data = FileHandle.standardInput.readDataToEndOfFile()
do {
    guard let input = try JSONSerialization.jsonObject(with: data) as? [String] else {
        throw NSError(domain: "MacMCPMemory", code: 1)
    }
    guard let embedding = NLEmbedding.sentenceEmbedding(for: .english) else {
        throw NSError(domain: "MacMCPMemory", code: 2)
    }
    let vectors: [[Double]] = input.map { text in
        guard let vector = embedding.vector(for: text) else { return [] }
        return vector
    }
    let output: [String: Any] = ["ok": true, "dimension": embedding.dimension, "vectors": vectors]
    let encoded = try JSONSerialization.data(withJSONObject: output)
    FileHandle.standardOutput.write(encoded)
} catch {
    let output: [String: Any] = ["ok": false, "error": String(describing: error)]
    let encoded = try JSONSerialization.data(withJSONObject: output)
    FileHandle.standardOutput.write(encoded)
    exit(1)
}
