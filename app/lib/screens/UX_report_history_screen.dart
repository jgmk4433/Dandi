import 'package:flutter/material.dart';
import 'package:flutter/foundation.dart' show debugPrint;
import '../services/api_service.dart';
import '../services/local_storage_service.dart';

class ReportHistoryScreen extends StatefulWidget {
  const ReportHistoryScreen({Key? key}) : super(key: key);
  @override
  State<ReportHistoryScreen> createState() => _ReportHistoryScreenState();
}

class _ReportHistoryScreenState extends State<ReportHistoryScreen> {
  final ApiService _apiService = ApiService();
  List<Map<String, dynamic>> _reportList = [];
  bool _isLoading = false;

  @override
  void initState() {
    super.initState();
    _loadHistory();
  }

  // 로컬 신고 내역을 불러오고 미발급 사건의 서버 상태를 동기화한다.
  Future<void> _loadHistory() async {
    setState(() => _isLoading = true);
    try {
      final reports = await LocalStorageService.getReports();

      for (final r in reports) {
        final traceId = r['traceId'] as String?;
        final eventNo = r['eventNo'] as String?;

        // 서버 추적이 불가능하거나 사건번호가 발급된 항목은 제외한다.
        if (traceId == null || traceId.isEmpty) continue;
        if (eventNo != null && eventNo.isNotEmpty) continue;

        try {
          final data = await _apiService.getEnforcementStatus(traceId);
          if (data == null) continue;

          final newEventNo = data['event_no'] as String?;
          final newStatus = data['status'] as String?;

          if (newEventNo != null || newStatus != null) {
            await LocalStorageService.updateReport(
              r['localId'] as String,
              eventNo: newEventNo,
              status: newStatus,
            );
          }
        } catch (e) {
          // 상태 조회 실패와 관계없이 로컬 목록은 표시한다.
          debugPrint('상태 조회 건너뜀 ($traceId): $e');
        }
      }

      // 동기화된 내역을 최신순으로 표시한다.
      final refreshed = await LocalStorageService.getReports();
      if (mounted) {
        setState(() => _reportList = refreshed.reversed.toList());
      }
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context)
            .showSnackBar(const SnackBar(content: Text('내역 불러오기 실패')));
      }
    } finally {
      if (mounted) setState(() => _isLoading = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('나의 신고 내역')),
      body: RefreshIndicator(
        onRefresh: _loadHistory,
        child: _isLoading
            ? const Center(child: CircularProgressIndicator())
            : _reportList.isEmpty
                ? ListView(
                    // 빈 목록에서도 당겨서 새로고침을 지원한다.
                    children: const [
                      SizedBox(height: 200),
                      Center(child: Text('아직 접수된 신고 내역이 없습니다.')),
                    ],
                  )
                : ListView.builder(
                    itemCount: _reportList.length,
                    itemBuilder: (context, index) {
                      final report = _reportList[index];

                      final String? eventNo = report['eventNo'] as String?;
                      final String? traceId = report['traceId'] as String?;
                      final String status =
                          (report['status'] as String?) ?? '알 수 없음';
                      final String? address = report['address'] as String?;

                      final String capturedRaw =
                          (report['capturedAt'] as String?) ?? '';
                      final String capturedDisplay = capturedRaw.length >= 16
                          ? capturedRaw.substring(0, 16).replaceFirst('T', ' ')
                          : capturedRaw;

                      // 사건번호 발급 단계에 맞는 제목을 구성한다.
                      final String titleText;
                      if (eventNo != null && eventNo.isNotEmpty) {
                        titleText = '사건번호: $eventNo';
                      } else if (traceId != null && traceId.isNotEmpty) {
                        titleText = '사건번호: 발급 대기 중';
                      } else {
                        titleText = '사건번호: 미발급 (허브 미전송)';
                      }

                      return Card(
                        margin: const EdgeInsets.symmetric(
                            horizontal: 16, vertical: 8),
                        child: ListTile(
                          leading: _getStatusIcon(status),
                          title: Text(
                            titleText,
                            style: const TextStyle(
                                fontSize: 14, fontWeight: FontWeight.bold),
                          ),
                          subtitle: Column(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: [
                              Text('상태: $status'),
                              if (traceId != null && traceId.isNotEmpty)
                                Text('추적 ID: $traceId',
                                    style: const TextStyle(
                                        fontSize: 12, color: Colors.black54)),
                              if (capturedDisplay.isNotEmpty)
                                Text('촬영: $capturedDisplay'),
                              Text(
                                '위치: ${(address != null && address.isNotEmpty) ? address : '정보 없음'}',
                                style: TextStyle(
                                  color: (address != null && address.isNotEmpty)
                                      ? Colors.black87
                                      : Colors.redAccent,
                                ),
                              ),
                            ],
                          ),
                        ),
                      );
                    },
                  ),
      ),
    );
  }

  // 신고 처리 상태에 대응하는 아이콘을 반환한다.
  Widget _getStatusIcon(String status) {
    // 서버 미전송 상태
    if (status.contains('전송 대기') || status.contains('미전송')) {
      return const Icon(Icons.cloud_off, color: Colors.grey);
    }
    // 서버 접수 완료 상태
    if (status.contains('수신 완료') || status.contains('ACCEPTED')) {
      return const Icon(Icons.cloud_done, color: Colors.blue);
    }
    // 서버 처리 중 상태
    if (status.contains('PROCESSING') || status.contains('처리 중')) {
      return const Icon(Icons.autorenew, color: Colors.orange);
    }
    // 단속 확정 상태
    if (status.contains('CONFIRMED')) {
      return const Icon(Icons.check_circle, color: Colors.green);
    }
    // 단속 취소 상태
    if (status.contains('CANCELLED')) {
      return const Icon(Icons.cancel, color: Colors.grey);
    }
    // 이의제기 접수 상태
    if (status.contains('APPEALED')) {
      return const Icon(Icons.gavel, color: Colors.deepOrange);
    }
    return const Icon(Icons.info, color: Colors.grey);
  }
}
