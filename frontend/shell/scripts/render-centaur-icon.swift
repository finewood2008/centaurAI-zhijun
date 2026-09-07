import Foundation
import CoreGraphics
import ImageIO

// No generated replacement art: only the original JPEG's near-white backdrop
// becomes transparent. Colored facets, pose, tail and all four legs are kept.
guard CommandLine.arguments.count == 4 else {
    fatalError("Usage: render-centaur-icon.swift source.jpg icon.png layout.json")
}
let canvas = 1024
let tileSize = 880.0
let tileRadius = 190.0
let illustrationSize = 736.0
let backgroundStart = 240
let backgroundClear = 250
let backgroundChroma = 18
let ivory: [CGFloat] = [1.0, 0.98, 0.945, 1.0]
let shadowOffset = -8.0
let shadowBlur = 16.0
let shadowOpacity = 0.16
let sourceURL = URL(fileURLWithPath: CommandLine.arguments[1])
guard let source = CGImageSourceCreateWithURL(sourceURL as CFURL, nil),
      let original = CGImageSourceCreateImageAtIndex(source, 0, nil),
      let colorSpace = CGColorSpace(name: CGColorSpace.sRGB) else {
    fatalError("Cannot decode the original centaur image")
}
let bitmapInfo = CGImageAlphaInfo.premultipliedLast.rawValue | CGBitmapInfo.byteOrder32Big.rawValue
var sourcePixels = [UInt8](repeating: 0, count: original.width * original.height * 4)
var transparentSourcePixels = 0
let cutout: CGImage = sourcePixels.withUnsafeMutableBytes { bytes in
    guard let context = CGContext(data: bytes.baseAddress, width: original.width, height: original.height,
        bitsPerComponent: 8, bytesPerRow: original.width * 4, space: colorSpace, bitmapInfo: bitmapInfo) else {
        fatalError("Cannot create the source bitmap")
    }
    context.draw(original, in: CGRect(x: 0, y: 0, width: original.width, height: original.height))
    let data = bytes.bindMemory(to: UInt8.self)
    for index in stride(from: 0, to: data.count, by: 4) {
        let channels = [Int(data[index]), Int(data[index + 1]), Int(data[index + 2])]
        let low = channels.min()!, high = channels.max()!
        if low >= backgroundStart && high - low <= backgroundChroma {
            // A narrow soft transition retains antialiased edges. Premultiply
            // the retained color because the CoreGraphics bitmap requires it.
            let alpha = max(0, min(255, (backgroundClear - low) * 255 / (backgroundClear - backgroundStart)))
            for channel in 0..<3 { data[index + channel] = UInt8(channels[channel] * alpha / 255) }
            data[index + 3] = UInt8(alpha)
            if alpha == 0 { transparentSourcePixels += 1 }
        }
    }
    guard let image = context.makeImage() else { fatalError("Cannot create the background-free centaur") }
    return image
}
guard let context = CGContext(data: nil, width: canvas, height: canvas, bitsPerComponent: 8,
    bytesPerRow: canvas * 4, space: colorSpace, bitmapInfo: bitmapInfo) else {
    fatalError("Cannot create the transparent icon canvas")
}
context.clear(CGRect(x: 0, y: 0, width: canvas, height: canvas))
let margin = (Double(canvas) - tileSize) / 2
let tile = CGPath(roundedRect: CGRect(x: margin, y: margin, width: tileSize, height: tileSize),
    cornerWidth: tileRadius, cornerHeight: tileRadius, transform: nil)
context.saveGState()
context.setShadow(offset: CGSize(width: 0, height: shadowOffset), blur: shadowBlur,
    color: CGColor(gray: 0, alpha: shadowOpacity))
context.setFillColor(CGColor(colorSpace: colorSpace, components: ivory)!)
context.addPath(tile)
context.fillPath()
context.restoreGState()
let scale = min(illustrationSize / Double(cutout.width), illustrationSize / Double(cutout.height))
let width = Double(cutout.width) * scale, height = Double(cutout.height) * scale
context.interpolationQuality = .high
context.draw(cutout, in: CGRect(x: (Double(canvas) - width) / 2,
    y: (Double(canvas) - height) / 2, width: width, height: height))
guard let rendered = context.makeImage(),
      let destination = CGImageDestinationCreateWithURL(URL(fileURLWithPath: CommandLine.arguments[2]) as CFURL,
        "public.png" as CFString, 1, nil) else { fatalError("Cannot create the icon PNG") }
CGImageDestinationAddImage(destination, rendered, nil)
guard CGImageDestinationFinalize(destination) else { fatalError("Cannot save the icon PNG") }
let layout: [String: Any] = [
    "canvas": canvas, "tileSize": tileSize, "tileRadius": tileRadius,
    "tileColorSRGB": ivory.map { Double($0) }, "illustrationMaxDimension": illustrationSize,
    "shadow": ["offsetY": shadowOffset, "blur": shadowBlur, "opacity": shadowOpacity],
    "backgroundMask": ["minimumChannelStart": backgroundStart, "minimumChannelClear": backgroundClear,
        "maximumChroma": backgroundChroma, "fullyTransparentSourcePixels": transparentSourcePixels],
    "sourceSize": ["width": original.width, "height": original.height],
]
try JSONSerialization.data(withJSONObject: layout, options: [.prettyPrinted, .sortedKeys])
    .write(to: URL(fileURLWithPath: CommandLine.arguments[3]))
