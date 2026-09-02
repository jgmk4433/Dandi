import 'dart:convert';
import 'dart:io';
import 'package:flutter/foundation.dart' show debugPrint;
import 'package:http/http.dart' as http;
import 'local_storage_service.dart';

// 중앙 허브 요청에 사용하는 인증 키다.
const String kHubApiKey = 'klpJo9jMNQn8GIBMKJk2jgpT7DR0JU0Xoxeyelye42k';

// 위치 정보 전송에 사용할 multipart 필드명을 정의한다.
const String kFieldLatitude = 'latitude';
const String kFieldLongitude = 'longitude';
const String kFieldAddress = 'address';

// 설정된 지역 코드의 전송 여부를 제어한다.
const bool kSendRegionCode = true;

// trace ID 기반 사건 상태 조회 경로다.
const String kStatusEndpointPath = '/api/v1/enforce/status';

// 단속 신고 전송 결과를 표현한다.
class SubmitResult {
  final String status;
  final String? traceId;
  final String? message;

  const SubmitResult({required this.status, this.traceId, this.message});
}

class ApiService {
  // 저장된 서버 주소를 요청 가능한 URL 형식으로 정규화한다.
  Future<String> _getBaseUrl() async {
    String? baseUrl = await LocalStorageService.getServerUrl();

    if (baseUrl == null || baseUrl.trim().isEmpty) {
      throw Exception('설정에서 중앙 허브 주소를 먼저 입력해주세요.');
    }

    baseUrl = baseUrl.trim();
    while (baseUrl!.endsWith('/')) {
      baseUrl = baseUrl.substring(0, baseUrl.length - 1);
    }

    if (!baseUrl.startsWith('http://') && !baseUrl.startsWith('https://')) {
      baseUrl = 'https://$baseUrl';
    }
    return baseUrl;
  }

  Map<String, String> get _authHeader => {'X-API-KEY': kHubApiKey};

  Map<String, String> get _jsonHeaders => {
        'X-API-KEY': kHubApiKey,
        'Content-Type': 'application/json',
      };

  // 촬영 시각을 서버 규격인 yyyy-MM-dd HH:mm:ss 형식으로 변환한다.
  static String formatTimestamp(DateTime dt) {
    String two(int v) => v.toString().padLeft(2, '0');
    return '${dt.year}-${two(dt.month)}-${two(dt.day)} '
        '${two(dt.hour)}:${two(dt.minute)}:${two(dt.second)}';
  }

  // 이미지와 촬영 정보를 multipart 형식으로 중앙 허브에 전송한다.
  Future<SubmitResult> submitEnforcement(
    File imageFile,
    DateTime capturedAt, {
    String? regionCode,
    String? address,
    double? latitude,
    double? longitude,
  }) async {
    final baseUrl = await _getBaseUrl();
    final uri = Uri.parse('$baseUrl/api/v1/enforce/submit');

    final request = http.MultipartRequest('POST', uri);
    request.headers.addAll(_authHeader);

    request.fields['timestamp'] = formatTimestamp(capturedAt);

    if (kSendRegionCode && regionCode != null && regionCode.isNotEmpty) {
      request.fields['region_code'] = regionCode;
    }

    // 존재하는 위치 정보만 요청 필드에 추가한다.
    if (latitude != null) {
      request.fields[kFieldLatitude] = latitude.toString();
    }
    if (longitude != null) {
      request.fields[kFieldLongitude] = longitude.toString();
    }
    if (address != null && address.isNotEmpty) {
      request.fields[kFieldAddress] = address;
    }

    request.files.add(await http.MultipartFile.fromPath('file', imageFile.path));

    debugPrint('[API] 전송 -> $uri (fields: ${request.fields})');

    final streamed = await request.send();
    final body = await streamed.stream.bytesToString();

    if (streamed.statusCode >= 200 && streamed.statusCode < 300) {
      final decoded = json.decode(body) as Map<String, dynamic>;
      final result = SubmitResult(
        status: (decoded['status'] as String?) ?? 'UNKNOWN',
        traceId: decoded['trace_id'] as String?,
        message: decoded['message'] as String?,
      );
      debugPrint('[API] 전송 성공: status=${result.status}, trace_id=${result.traceId}');
      return result;
    }

    debugPrint('[API] 전송 실패 ${streamed.statusCode}: $body');
    throw Exception('전송 실패 (${streamed.statusCode})');
  }

  // trace ID로 사건번호와 처리 상태를 조회한다.
  Future<Map<String, dynamic>?> getEnforcementStatus(String traceId) async {
    final baseUrl = await _getBaseUrl();
    final uri = Uri.parse('$baseUrl$kStatusEndpointPath/$traceId');

    final response = await http.get(uri, headers: _authHeader);

    if (response.statusCode >= 200 && response.statusCode < 300) {
      return json.decode(utf8.decode(response.bodyBytes)) as Map<String, dynamic>;
    }

    // 조회 실패 시 호출부가 로컬 내역을 유지하도록 null을 반환한다.
    debugPrint('[API] 상태 조회 실패 ${response.statusCode} (미구현 API일 수 있음)');
    return null;
  }

  // 사건번호로 이의제기 대상 사건 정보를 조회한다.
  Future<Map<String, dynamic>> inquireEvent(String eventNo) async {
    final baseUrl = await _getBaseUrl();
    final uri = Uri.parse('$baseUrl/api/v1/appeal/inquire/$eventNo');

    final response = await http.get(uri, headers: _authHeader);

    if (response.statusCode >= 200 && response.statusCode < 300) {
      final decoded = json.decode(utf8.decode(response.bodyBytes));
      if (decoded is Map<String, dynamic>) {
        // 래핑된 data 객체와 최상위 객체 응답을 모두 처리한다.
        final data = decoded['data'];
        if (data is Map<String, dynamic>) return data;
        return decoded;
      }
      throw Exception('예상치 못한 응답 형식');
    }

    debugPrint('[API] 사건 조회 실패 ${response.statusCode}: ${response.body}');
    throw Exception('조회 실패 (${response.statusCode})');
  }

  // 사건번호와 사유를 JSON 형식으로 전송해 이의제기를 접수한다.
  Future<Map<String, dynamic>> submitAppeal(String eventNo, String reason) async {
    final baseUrl = await _getBaseUrl();
    final uri = Uri.parse('$baseUrl/api/v1/appeal/submit');

    final response = await http.post(
      uri,
      headers: _jsonHeaders,
      body: json.encode({
        'event_no': eventNo,
        'appeal_reason': reason,
      }),
    );

    if (response.statusCode >= 200 && response.statusCode < 300) {
      return json.decode(utf8.decode(response.bodyBytes)) as Map<String, dynamic>;
    }

    debugPrint('[API] 이의제기 실패 ${response.statusCode}: ${response.body}');
    throw Exception('이의제기 실패 (${response.statusCode})');
  }
}
