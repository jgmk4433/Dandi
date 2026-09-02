import 'dart:convert';
import 'package:shared_preferences/shared_preferences.dart';

class LocalStorageService {
  static const String _keyServerUrl = 'server_url';
  static const String _keyRegionCode = 'region_code';
  static const String _keyReports = 'reports_v2';

  // 전체 삭제 시 함께 정리할 이전 버전 저장 키다.
  static const String _keyLegacyMyReports = 'my_reports';
  static const String _keyLegacyOffline = 'offline_reports';

  // 서버 URL을 로컬 설정에서 조회한다.
  static Future<String?> getServerUrl() async {
    final prefs = await SharedPreferences.getInstance();
    return prefs.getString(_keyServerUrl);
  }

  // 서버 URL을 로컬 설정에 저장한다.
  static Future<void> setServerUrl(String url) async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString(_keyServerUrl, url);
  }

  // 저장된 지역 코드를 조회한다.
  static Future<String?> getRegionCode() async {
    final prefs = await SharedPreferences.getInstance();
    return prefs.getString(_keyRegionCode);
  }

  // 지역 코드를 정규화해 저장한다.
  static Future<void> setRegionCode(String code) async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString(_keyRegionCode, code.trim().toUpperCase());
  }

  // JSON 문자열로 저장된 신고 레코드 목록을 복원한다.
  static Future<List<Map<String, dynamic>>> getReports() async {
    final prefs = await SharedPreferences.getInstance();
    final list = prefs.getStringList(_keyReports) ?? [];
    return list
        .map((s) => Map<String, dynamic>.from(jsonDecode(s) as Map))
        .toList();
  }

  // 신고 레코드를 JSON으로 직렬화해 목록에 추가한다.
  static Future<void> addReport(Map<String, dynamic> report) async {
    final prefs = await SharedPreferences.getInstance();
    final list = prefs.getStringList(_keyReports) ?? [];
    list.add(jsonEncode(report));
    await prefs.setStringList(_keyReports, list);
  }

  // localId가 일치하는 신고 레코드의 전달된 필드만 갱신한다.
  static Future<void> updateReport(
    String localId, {
    String? traceId,
    String? eventNo,
    String? status,
  }) async {
    final prefs = await SharedPreferences.getInstance();
    final list = prefs.getStringList(_keyReports) ?? [];

    final updated = list.map((s) {
      final map = Map<String, dynamic>.from(jsonDecode(s) as Map);
      if (map['localId'] != localId) return s;

      if (traceId != null) map['traceId'] = traceId;
      if (eventNo != null) map['eventNo'] = eventNo;
      if (status != null) map['status'] = status;
      return jsonEncode(map);
    }).toList();

    await prefs.setStringList(_keyReports, updated);
  }

  // 현재 및 이전 버전의 신고 기록을 모두 삭제한다.
  static Future<void> clearReports() async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.remove(_keyReports);
    await prefs.remove(_keyLegacyMyReports);
    await prefs.remove(_keyLegacyOffline);
  }
}
