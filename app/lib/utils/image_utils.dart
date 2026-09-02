// lib/utils/image_utils.dart
import 'package:image/image.dart' as img;

// 이미지 후처리 유틸리티 클래스
class ImageUtils {
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