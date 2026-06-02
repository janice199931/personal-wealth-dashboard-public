import Foundation
import Vision
import AppKit

if CommandLine.arguments.count < 2 {
    fputs("Usage: swift scripts/ocr_image.swift /path/to/image\n", stderr)
    exit(2)
}

let imageURL = URL(fileURLWithPath: CommandLine.arguments[1])
guard let image = NSImage(contentsOf: imageURL),
      let cgImage = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
    fputs("Cannot read image\n", stderr)
    exit(1)
}

let request = VNRecognizeTextRequest { request, error in
    if let error = error {
        fputs("OCR failed: \(error.localizedDescription)\n", stderr)
        exit(1)
    }

    let observations = request.results as? [VNRecognizedTextObservation] ?? []
    let lines = observations.compactMap { observation in
        observation.topCandidates(1).first?.string
    }
    print(lines.joined(separator: "\n"))
}

request.recognitionLanguages = ["zh-Hant", "en-US"]
request.recognitionLevel = .accurate
request.usesLanguageCorrection = true

let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])
do {
    try handler.perform([request])
} catch {
    fputs("OCR handler failed: \(error.localizedDescription)\n", stderr)
    exit(1)
}
