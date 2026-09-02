// lib/utils/image_utils.dart
import 'package:image/image.dart' as img;

// 이미지 위에서 선명하게 유지할 사각 영역을 표현한다.
class KeepRect {
  final int x;
  final int y;
  final int width;
  final int height;

  const KeepRect({
    required this.x,
    required this.y,
    required this.width,
    required this.height,
  });

  // 이미지 전체가 유지 영역인지 판단한다.
  bool coversAll(img.Image image) =>
      x <= 0 && y <= 0 && width >= image.width && height >= image.height;

  @override
  String toString() => '($x,$y ${width}x$height)';
}

// 이미지 후처리 유틸리티 클래스
class ImageUtils {
  // 가이드 박스 비율과 여백 배율로 중앙 유지 영역을 계산한다.
  static KeepRect calcKeepRect(
    img.Image image, {
    required double widthFactor,
    required double heightFactor,
    required double marginFactor,
  }) {
    // 여백을 적용한 유지 비율이 이미지를 벗어나지 않도록 제한한다.
    final double keepW = (widthFactor * marginFactor).clamp(0.01, 1.0);
    final double keepH = (heightFactor * marginFactor).clamp(0.01, 1.0);

    int w = (image.width * keepW).round();
    int h = (image.height * keepH).round();
    if (w < 1) w = 1;
    if (h < 1) h = 1;
    if (w > image.width) w = image.width;
    if (h > image.height) h = image.height;

    final int x = ((image.width - w) / 2).round();
    final int y = ((image.height - h) / 2).round();

    return KeepRect(x: x, y: y, width: w, height: h);
  }

  // 이미지 크기에 비례하는 가우시안 블러 반경을 산출한다.
  static int autoBlurRadius(img.Image image) {
    final int longSide =
        image.width > image.height ? image.width : image.height;
    final int radius = (longSide * 0.012).round();
    if (radius < 6) return 6;
    if (radius > 40) return 40;
    return radius;
  }

  // 유지 영역을 제외한 주변부를 블러 처리한다.
  static img.Image blurOutsideRect(
    img.Image source,
    KeepRect keep, {
    int? blurRadius,
  }) {
    // 유지 영역이 이미지 전체면 블러 처리를 건너뛴다.
    if (keep.coversAll(source)) return source;

    final int radius = blurRadius ?? autoBlurRadius(source);

    // 원본을 보존하기 위해 복제본을 전면 블러 처리한다.
    final blurred = img.gaussianBlur(img.Image.from(source), radius: radius);

    // 유지 영역만 원본 픽셀로 되돌려 합성한다.
    final sharp = img.copyCrop(
      source,
      x: keep.x,
      y: keep.y,
      width: keep.width,
      height: keep.height,
    );
    img.compositeImage(blurred, sharp, dstX: keep.x, dstY: keep.y);

    return blurred;
  }

  // 촬영 시각 텍스트 합성 처리
  static Future<img.Image> burnTimestamp(img.Image decoded, DateTime captureTime) async {
    final timestampText =
        '${captureTime.year}-${_pad(captureTime.month)}-${_pad(captureTime.day)} '
        '${_pad(captureTime.hour)}:${_pad(captureTime.minute)}:${_pad(captureTime.second)}';

    img.drawString(
      decoded,
      timestampText,
      font: img.arial48,
      x: 20,
      y: 20,
      color: img.ColorRgb8(255, 0, 0),
    );

    return decoded;
  }

  // 숫자 2자리 포맷팅 헬퍼 메서드
  static String _pad(int value) => value.toString().padLeft(2, '0');
}
