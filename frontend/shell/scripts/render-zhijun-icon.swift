import Foundation
import CoreGraphics
import ImageIO

// Draw the same font-independent paths used by the sidebar and browser icon.
guard CommandLine.arguments.count == 3 else { fatalError("Usage: render-zhijun-icon.swift mark.json icon.png") }
let data = try Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1]))
let mark = try JSONSerialization.jsonObject(with: data) as! [String: Any]
let paths = mark["paths"] as! [[[Any]]]
let colorSpace = CGColorSpace(name: CGColorSpace.sRGB)!
func color(_ hex: String) -> CGColor {
    let value = UInt32(hex.dropFirst(), radix: 16)!
    return CGColor(colorSpace: colorSpace, components: [CGFloat((value >> 16) & 255) / 255,
        CGFloat((value >> 8) & 255) / 255, CGFloat(value & 255) / 255, 1])!
}
let context = CGContext(data: nil, width: 1024, height: 1024, bitsPerComponent: 8, bytesPerRow: 4096,
    space: colorSpace, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue | CGBitmapInfo.byteOrder32Big.rawValue)!
context.clear(CGRect(x: 0, y: 0, width: 1024, height: 1024))
context.saveGState()
context.setShadow(offset: CGSize(width: 0, height: -8), blur: 16, color: CGColor(gray: 0, alpha: 0.12))
context.setFillColor(color(mark["background"] as! String))
context.addPath(CGPath(roundedRect: CGRect(x: 72, y: 72, width: 880, height: 880), cornerWidth: 190, cornerHeight: 190, transform: nil))
context.fillPath()
context.restoreGState()
context.translateBy(x: 0, y: 1024)
context.scaleBy(x: 10.24, y: -10.24)
context.setFillColor(color(mark["color"] as! String))
for commands in paths {
    let path = CGMutablePath()
    for command in commands {
        let n = command.dropFirst().map { ($0 as! NSNumber).doubleValue }
        switch command[0] as! String {
        case "M": path.move(to: CGPoint(x: n[0], y: n[1]))
        case "L": path.addLine(to: CGPoint(x: n[0], y: n[1]))
        case "C": path.addCurve(to: CGPoint(x: n[4], y: n[5]), control1: CGPoint(x: n[0], y: n[1]), control2: CGPoint(x: n[2], y: n[3]))
        case "Q": path.addQuadCurve(to: CGPoint(x: n[2], y: n[3]), control: CGPoint(x: n[0], y: n[1]))
        case "Z": path.closeSubpath()
        default: fatalError("Unsupported vector command")
        }
    }
    context.addPath(path)
    context.fillPath()
}
let image = context.makeImage()!
let destination = CGImageDestinationCreateWithURL(URL(fileURLWithPath: CommandLine.arguments[2]) as CFURL, "public.png" as CFString, 1, nil)!
CGImageDestinationAddImage(destination, image, nil)
guard CGImageDestinationFinalize(destination) else { fatalError("Cannot save icon") }
