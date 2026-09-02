import 'package:flutter/foundation.dart' show debugPrint;
import 'package:geolocator/geolocator.dart';
import 'package:geocoding/geocoding.dart';

// GPS 좌표와 변환된 주소를 함께 전달한다.
class LocationResult {
  final double latitude;
  final double longitude;
  final String address;

  LocationResult({
    required this.latitude,
    required this.longitude,
    required this.address,
  });
}

class LocationService {
  // 위치 권한을 확인하고 현재 좌표와 주소를 조회한다.
  static Future<LocationResult?> getCurrentLocationWithAddress() async {
    try {
      // 단말 위치 서비스 활성화 여부를 확인한다.
      final bool serviceEnabled = await Geolocator.isLocationServiceEnabled();
      if (!serviceEnabled) {
        debugPrint('[위치] 실패: 단말의 위치 서비스(GPS)가 꺼져 있습니다.');
        return null;
      }

      // 위치 권한이 없으면 사용자에게 요청한다.
      LocationPermission permission = await Geolocator.checkPermission();
      if (permission == LocationPermission.denied) {
        permission = await Geolocator.requestPermission();
        if (permission == LocationPermission.denied) {
          debugPrint('[위치] 실패: 사용자가 위치 권한을 거부했습니다.');
          return null;
        }
      }
      if (permission == LocationPermission.deniedForever) {
        // 영구 거부 상태는 기기 설정에서 직접 허용해야 한다.
        debugPrint('[위치] 실패: 위치 권한이 영구 거부됨(다시 묻지 않음). '
            '기기 설정 > 앱 > 권한에서 직접 허용해야 합니다.');
        return null;
      }

      // 최대 10초 동안 고정밀 GPS 좌표를 조회한다.
      final position = await Geolocator.getCurrentPosition(
        locationSettings: const LocationSettings(
          accuracy: LocationAccuracy.high,
          timeLimit: Duration(seconds: 10),
        ),
      );

      debugPrint('[위치] 좌표 획득 성공: '
          '${position.latitude}, ${position.longitude}');

      // 좌표를 행정구역 주소로 변환한다.
      final address = await _reverseGeocode(position.latitude, position.longitude);
      debugPrint('[위치] 주소 변환 결과: $address');

      return LocationResult(
        latitude: position.latitude,
        longitude: position.longitude,
        address: address,
      );
    } catch (e) {
      // 위치 조회 실패 시 신고 흐름을 유지하도록 null을 반환한다.
      debugPrint('[위치] 실패(예외): $e');
      return null;
    }
  }

  // 좌표를 행정구역 순서의 주소 문자열로 변환한다.
  static Future<String> _reverseGeocode(double lat, double lng) async {
    try {
      final placemarks = await placemarkFromCoordinates(lat, lng);
      if (placemarks.isEmpty) {
        debugPrint('[위치] 역지오코딩 결과가 비어 있습니다.');
        return '주소 확인 불가';
      }

      final p = placemarks.first;

      // 비어 있지 않은 행정구역 항목만 순서대로 결합한다.
      final parts = <String>[
        if ((p.administrativeArea ?? '').isNotEmpty) p.administrativeArea!,
        if ((p.subAdministrativeArea ?? '').isNotEmpty) p.subAdministrativeArea!,
        if ((p.locality ?? '').isNotEmpty &&
            p.locality != p.subAdministrativeArea)
          p.locality!,
        if ((p.subLocality ?? '').isNotEmpty) p.subLocality!,
        if ((p.thoroughfare ?? '').isNotEmpty) p.thoroughfare!,
      ];

      if (parts.isEmpty) {
        return (p.name != null && p.name!.isNotEmpty) ? p.name! : '주소 확인 불가';
      }
      return parts.join(' ');
    } catch (e) {
      debugPrint('[위치] 역지오코딩 실패: $e');
      return '주소 확인 불가';
    }
  }
}
