import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:flutter/foundation.dart' show debugPrint;
import '../services/api_service.dart';
import '../services/local_storage_service.dart';

class AppealScreen extends StatefulWidget {
  const AppealScreen({Key? key}) : super(key: key);
  @override
  State<AppealScreen> createState() => _AppealScreenState();
}

class _AppealScreenState extends State<AppealScreen> {
  final ApiService _apiService = ApiService();
  final TextEditingController _eventNoController = TextEditingController();
  final TextEditingController _reasonController = TextEditingController();

  bool _isLoading = false;
  Map<String, dynamic>? _recordData;
  String _baseUrl = "";

  @override
  void initState() {
    super.initState();
    _fetchBaseUrl();
  }

  // 저장된 서버 URL을 이미지 요청에 사용할 형식으로 정규화한다.
  Future<void> _fetchBaseUrl() async {
    String url = (await LocalStorageService.getServerUrl()) ?? "";
    url = url.trim();
    while (url.endsWith('/')) {
      url = url.substring(0, url.length - 1);
    }
    if (url.isNotEmpty &&
        !url.startsWith('http://') &&
        !url.startsWith('https://')) {
      url = 'https://$url';
    }
    if (mounted) setState(() => _baseUrl = url);
  }

  // 사건번호로 단속 원본 데이터와 상태를 조회한다.
  Future<void> _searchRecord() async {
    final eventNo = _eventNoController.text.trim();
    if (eventNo.isEmpty) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('사건번호를 입력해주세요. (예: PM-DAEGU-20260731-A1B2)')),
      );
      return;
    }

    setState(() => _isLoading = true);
    try {
      final data = await _apiService.inquireEvent(eventNo);
      if (mounted) setState(() => _recordData = data);
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context)
            .showSnackBar(SnackBar(content: Text('조회 실패: $e')));
      }
    } finally {
      if (mounted) setState(() => _isLoading = false);
    }
  }

  // 입력된 사유로 이의제기를 접수하고 사건 상태를 갱신한다.
  Future<void> _submitAppeal() async {
    final eventNo = _eventNoController.text.trim();
    if (eventNo.isEmpty) return;

    final reason = _reasonController.text.trim();
    if (reason.isEmpty) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('이의제기 사유를 입력해주세요.')),
      );
      return;
    }

    setState(() => _isLoading = true);
    try {
      await _apiService.submitAppeal(eventNo, reason);
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text('이의제기가 접수되었습니다. 수동 검수로 재배정됩니다.')),
        );
        _reasonController.clear();
      }
      await _searchRecord(); // 접수 결과를 반영한다.
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context)
            .showSnackBar(SnackBar(content: Text('제출 실패: $e')));
      }
    } finally {
      if (mounted) setState(() => _isLoading = false);
    }
  }

  // 응답 형식에 따라 base64, URL 또는 파일명 기반 사건 이미지를 표시한다.
  Widget _buildEventImage() {
    final data = _recordData;
    if (data == null) return const SizedBox.shrink();

    // base64 이미지 데이터를 디코딩한다.
    final base64Str = (data['image_base64'] ?? data['imageBase64']) as String?;
    if (base64Str != null && base64Str.isNotEmpty) {
      try {
        // data URI 접두사를 제거한다.
        final pure = base64Str.contains(',')
            ? base64Str.substring(base64Str.indexOf(',') + 1)
            : base64Str;
        return ClipRRect(
          borderRadius: BorderRadius.circular(8),
          child: Image.memory(
            base64Decode(pure),
            height: 250,
            width: double.infinity,
            fit: BoxFit.cover,
          ),
        );
      } catch (e) {
        debugPrint('base64 이미지 디코딩 실패: $e');
        return const Icon(Icons.broken_image, size: 80);
      }
    }

    // 전체 이미지 URL을 직접 로드한다.
    final imageUrl = (data['image_url'] ?? data['imageUrl']) as String?;
    if (imageUrl != null && imageUrl.isNotEmpty) {
      return ClipRRect(
        borderRadius: BorderRadius.circular(8),
        child: Image.network(
          imageUrl,
          height: 250,
          width: double.infinity,
          fit: BoxFit.cover,
          errorBuilder: (_, __, ___) => const Icon(Icons.broken_image, size: 80),
        ),
      );
    }

    // 파일명 응답은 저장된 서버 URL과 조합한다.
    final imageFile = data['image_file'] as String?;
    if (imageFile != null && imageFile.isNotEmpty && _baseUrl.isNotEmpty) {
      return ClipRRect(
        borderRadius: BorderRadius.circular(8),
        child: Image.network(
          '$_baseUrl/images/$imageFile',
          height: 250,
          width: double.infinity,
          fit: BoxFit.cover,
          errorBuilder: (_, __, ___) => const Icon(Icons.broken_image, size: 80),
        ),
      );
    }

    return const SizedBox.shrink();
  }

  @override
  void dispose() {
    _eventNoController.dispose();
    _reasonController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final data = _recordData;

    return Scaffold(
      appBar: AppBar(title: const Text('이의제기')),
      body: SingleChildScrollView(
        padding: const EdgeInsets.all(16.0),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            const Text(
              '단속 내역 조회',
              style: TextStyle(
                  fontSize: 18, fontWeight: FontWeight.bold, color: Colors.black),
            ),
            const SizedBox(height: 8),
            Row(
              children: [
                Expanded(
                  child: TextField(
                    controller: _eventNoController,
                    decoration: const InputDecoration(
                      hintText: '사건번호 (예: PM-DAEGU-20260731-A1B2)',
                      border: OutlineInputBorder(),
                    ),
                  ),
                ),
                const SizedBox(width: 8),
                ElevatedButton(
                  onPressed: _isLoading ? null : _searchRecord,
                  style: ElevatedButton.styleFrom(
                    backgroundColor: Colors.lightGreen,
                    foregroundColor: Colors.white,
                    padding: const EdgeInsets.symmetric(vertical: 20),
                  ),
                  child: const Text('조회'),
                ),
              ],
            ),
            const SizedBox(height: 24),
            if (_isLoading)
              const Center(child: CircularProgressIndicator())
            else if (data != null) ...[
              const Divider(),
              const SizedBox(height: 16),
              Text(
                '상태: ${data['status'] ?? '알 수 없음'}',
                style: const TextStyle(
                    fontSize: 16, fontWeight: FontWeight.bold, color: Colors.blue),
              ),
              if (data['timestamp'] != null) ...[
                const SizedBox(height: 4),
                Text('단속 시각: ${data['timestamp']}'),
              ],
              if (data['violation_types'] != null) ...[
                const SizedBox(height: 4),
                Text('위반 항목: ${data['violation_types']}'),
              ],
              const SizedBox(height: 16),
              _buildEventImage(),
              const SizedBox(height: 24),
              const Text(
                '이의제기 사유',
                style: TextStyle(fontWeight: FontWeight.bold, color: Colors.black),
              ),
              const SizedBox(height: 8),
              TextField(
                controller: _reasonController,
                maxLines: 4,
                decoration: const InputDecoration(
                  hintText: '예: 헬멧 착용 상태입니다. 재검토 바랍니다.',
                  border: OutlineInputBorder(),
                ),
              ),
              const SizedBox(height: 16),
              SizedBox(
                width: double.infinity,
                child: ElevatedButton(
                  onPressed: _isLoading ? null : _submitAppeal,
                  style: ElevatedButton.styleFrom(
                    backgroundColor: Colors.redAccent,
                    foregroundColor: Colors.white,
                    padding: const EdgeInsets.symmetric(vertical: 16),
                  ),
                  child: const Text('이의제기 신청 (재검토 요청)'),
                ),
              ),
            ],
          ],
        ),
      ),
    );
  }
}
