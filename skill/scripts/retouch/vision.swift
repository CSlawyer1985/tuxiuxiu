import Foundation
import Vision
import CoreImage
import ImageIO
import UniformTypeIdentifiers

func box(_ r: CGRect) -> [Double] {
    [Double(r.minX), Double(1-r.maxY), Double(r.maxX), Double(1-r.minY)]
}
let args=CommandLine.arguments
guard args.count==3, let src=CGImageSourceCreateWithURL(URL(fileURLWithPath:args[1]) as CFURL,nil),
      let image=CGImageSourceCreateImageAtIndex(src,0,nil) else { exit(2) }
let handler=VNImageRequestHandler(cgImage:image,options:[:])
var result: [String:Any]=["faces":[[Double]](),"hands":[[Double]](),"features":[[[Double]]](),"protect":[[[Double]]]()]
if #available(macOS 12.0, *) {
    let request=VNDetectHumanRectanglesRequest();request.upperBodyOnly=false
    do {
        try handler.perform([request])
        result["people"]=(request.results ?? []).filter{$0.confidence>=0.3}.map{box($0.boundingBox)}
    } catch {result["people_error"]="full-body localization unavailable"}
}
let faceRequest=VNDetectFaceLandmarksRequest()
do {
    try handler.perform([faceRequest])
    let faces=faceRequest.results ?? []
    result["faces"]=faces.map { box($0.boundingBox) }
    var features=[[[Double]]]()
    for face in faces {
        let b=face.boundingBox
        let parts=[face.landmarks?.leftEye,face.landmarks?.rightEye,face.landmarks?.leftEyebrow,
                   face.landmarks?.rightEyebrow,face.landmarks?.outerLips]
        for part in parts {
            guard let part=part else {continue}
            let points=part.normalizedPoints.map { p in
                [Double(b.minX+CGFloat(p.x)*b.width),Double(1-(b.minY+CGFloat(p.y)*b.height))]
            }
            if points.count>=3 {features.append(points)}
        }
    }
    result["features"]=features
} catch {result["face_error"]="face localization unavailable"}
if #available(macOS 11.0, *) {
    let request=VNDetectHumanHandPoseRequest();request.maximumHandCount=16
    do {
        try handler.perform([request]);var hands=[[Double]]()
        for hand in request.results ?? [] {
            let points=try hand.recognizedPoints(.all).values.filter{$0.confidence>0.35}
            if points.count<5 {continue}
            let xs=points.map{Double($0.location.x)},ys=points.map{Double(1-$0.location.y)}
            hands.append([max(0,xs.min()!-0.015),max(0,ys.min()!-0.015),min(1,xs.max()!+0.015),min(1,ys.max()!+0.015)])
        }
        result["hands"]=hands
    } catch {result["hand_error"]="hand localization unavailable"}
}
if #available(macOS 12.0, *) {
    let request=VNGeneratePersonSegmentationRequest();request.qualityLevel = .accurate
    request.outputPixelFormat=kCVPixelFormatType_OneComponent8
    do {
        try handler.perform([request])
        if let observation=request.results?.first {
            let ci=CIImage(cvPixelBuffer:observation.pixelBuffer)
            if let cg=CIContext().createCGImage(ci,from:ci.extent),
               let dest=CGImageDestinationCreateWithURL(URL(fileURLWithPath:args[2]) as CFURL,UTType.png.identifier as CFString,1,nil) {
                CGImageDestinationAddImage(dest,cg,nil);CGImageDestinationFinalize(dest)
            }
        }
    } catch {result["person_error"]="person foreground candidate unavailable"}
}
let textRequest=VNDetectTextRectanglesRequest()
do {
    try handler.perform([textRequest]);let faces=result["faces"] as? [[Double]] ?? []
    result["protect"]=(textRequest.results ?? []).compactMap { observation -> [[Double]]? in
        let b=box(observation.boundingBox),cx=(b[0]+b[2])/2,cy=(b[1]+b[3])/2
        if faces.contains(where:{cx>$0[0] && cx<$0[2] && cy>$0[1] && cy<$0[3]}) {return nil}
        return [[b[0],b[1]],[b[2],b[1]],[b[2],b[3]],[b[0],b[3]]]
    }
} catch {result["text_error"]="text rectangles unavailable"}
let data=try JSONSerialization.data(withJSONObject:result,options:[.sortedKeys])
print(String(data:data,encoding:.utf8)!)
