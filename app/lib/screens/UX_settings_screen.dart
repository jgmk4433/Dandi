import 'dart:io';
import 'package:flutter/material.dart';
import 'package:flutter/foundation.dart' show debugPrint;
import '../services/local_storage_service.dart';

class SettingsScreen extends StatefulWidget {
  const SettingsScreen({Key? key}) : super(key: key);

  @override
  State<SettingsScreen> createState() => _SettingsScreenState();
}

class _SettingsScreenState extends State<SettingsScreen> {
  final TextEditingController _serverUrlController = TextEditingController();
  final TextEditingController _regionCodeController = TextEditingController();

  @override
  void initState() {
    super.initState();
    _loadSavedUrl(); // 저장된 설정을 화면에 반영한다.
  }

  // 저장된 서버 URL과 지역 코드를 불러온다.
  Future<void> _loadSavedUrl() async {
    final savedUrl = await LocalStorageService.getServerUrl();
    final savedRegion = await LocalStorageService.getRegionCode();
    if (!mounted) return;
    setState(() {
      if (savedUrl != null) _serverUrlController.text = savedUrl;
      if (savedRegion != null) _regionCodeController.text = savedRegion;
    });
  }

  // 입력한 지역 코드를 기기에 저장한다.
  Future<void> _saveRegionCode() async {
    final code = _regionCodeController.text.trim();
    await LocalStorageService.setRegionCode(code);
    if (mounted) {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text(code.isEmpty ? '지역코드를 비웠습니다.' : '지역코드 저장: ${code.toUpperCase()}')),
      );
    }
  }

  // 입력한 서버 URL을 기기에 저장한다.
  Future<void> _saveUrl() async {
    final currentUrl = _serverUrlController.text.trim();
    if (currentUrl.isNotEmpty) {
      await LocalStorageService.setServerUrl(currentUrl);
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('저장되었습니다.')));
      }
    }
  }

  // 사용자 확인 후 로컬 신고 기록과 저장 사진을 모두 삭제한다.
  Future<void> _clearAllRecords() async {
    // 삭제 전 사용자 확인을 받는다.
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('신고 기록 전체 삭제'),
        content: const Text(
          '이 기기에 저장된 신고 기록과 사진을 모두 삭제합니다.\n'
          '되돌릴 수 없습니다. 계속하시겠습니까?\n\n'
          '(중앙서버에 이미 접수된 신고 자체는 삭제되지 않습니다)',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(ctx, false),
            child: const Text('취소'),
          ),
          TextButton(
            onPressed: () => Navigator.pop(ctx, true),
            style: TextButton.styleFrom(foregroundColor: Colors.redAccent),
            child: const Text('삭제'),
          ),
        ],
      ),
    );

    if (confirmed != true) return;

    int deletedFiles = 0;
    int deletedRecords = 0;

    try {
      // 신고 기록에 연결된 사진 파일을 먼저 삭제한다.
      final reports = await LocalStorageService.getReports();
      deletedRecords = reports.length;

      for (final r in reports) {
        final path = r['imagePath'] as String?;
        if (path == null || path.isEmpty) continue;
        try {
          final f = File(path);
          if (await f.exists()) {
            await f.delete();
            deletedFiles++;
          }
        } catch (e) {
          debugPrint('사진 삭제 실패(무시): $e');
        }
      }

      // 사진 정리 후 로컬 신고 목록을 비운다.
      await LocalStorageService.clearReports();

      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(
            content: Text('삭제 완료 - 기록 $deletedRecords건, 사진 $deletedFiles개'),
          ),
        );
      }
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context)
            .showSnackBar(SnackBar(content: Text('삭제 실패: $e')));
      }
    }
  }

  @override
  void dispose() {
    _serverUrlController.dispose();
    _regionCodeController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('설정')),
      body: ListView(
        padding: const EdgeInsets.all(16.0),
        children: [
          const Text(
            '서버 설정',
            style: TextStyle(fontSize: 16, fontWeight: FontWeight.bold, color: Colors.black),
          ),
          const SizedBox(height: 8),
          TextField(
            controller: _serverUrlController,
            decoration: InputDecoration(
              hintText: '서버 주소 (예: https://your-tunnel.trycloudflare.com)',
              filled: true,
              fillColor: Colors.white,
              focusedBorder: const OutlineInputBorder(
                borderSide: BorderSide(color: Colors.lightGreen, width: 2),
              ),
              enabledBorder: const OutlineInputBorder(
                borderSide: BorderSide(color: Colors.grey, width: 1),
              ),
              suffixIcon: IconButton(
                icon: const Icon(Icons.save, color: Colors.lightGreen),
                onPressed: _saveUrl, 
              ),
            ),
          ),
          
          const SizedBox(height: 24),
          const Text(
            '지역 코드',
            style: TextStyle(fontSize: 16, fontWeight: FontWeight.bold, color: Colors.black),
          ),
          const SizedBox(height: 4),
          const Text(
            '전송 시 region_code로 함께 보냅니다. 중앙 허브가 위치로 관할을 '
            '자동 배정한다면 비워두세요.',
            style: TextStyle(fontSize: 12, color: Colors.black54),
          ),
          const SizedBox(height: 8),
          TextField(
            controller: _regionCodeController,
            textCapitalization: TextCapitalization.characters,
            decoration: InputDecoration(
              hintText: '예: DAEGU',
              filled: true,
              fillColor: Colors.white,
              focusedBorder: const OutlineInputBorder(
                borderSide: BorderSide(color: Colors.lightGreen, width: 2),
              ),
              enabledBorder: const OutlineInputBorder(
                borderSide: BorderSide(color: Colors.grey, width: 1),
              ),
              suffixIcon: IconButton(
                icon: const Icon(Icons.save, color: Colors.lightGreen),
                onPressed: _saveRegionCode,
              ),
            ),
          ),

          const SizedBox(height: 32),
          const Divider(),

          const SizedBox(height: 8),
          const Text(
            '데이터 관리',
            style: TextStyle(fontSize: 16, fontWeight: FontWeight.bold, color: Colors.black),
          ),
          const SizedBox(height: 4),
          const Text(
            '테스트로 쌓인 기록을 중앙서버 연동 전에 비울 때 사용합니다.',
            style: TextStyle(fontSize: 12, color: Colors.black54),
          ),
          const SizedBox(height: 12),
          SizedBox(
            width: double.infinity,
            child: OutlinedButton.icon(
              onPressed: _clearAllRecords,
              icon: const Icon(Icons.delete_outline),
              label: const Text('신고 기록 전체 삭제'),
              style: OutlinedButton.styleFrom(
                foregroundColor: Colors.redAccent,
                side: const BorderSide(color: Colors.redAccent),
                padding: const EdgeInsets.symmetric(vertical: 14),
              ),
            ),
          ),

          const SizedBox(height: 24),
          const Divider(),

          ListTile(
            contentPadding: EdgeInsets.zero,
            leading: const Icon(Icons.info, color: Colors.black),
            title: const Text('앱 버전 정보', style: TextStyle(color: Colors.black)),
            trailing: const Text('v1.0.0', style: TextStyle(color: Colors.black54)),
          ),
        ],
      ),
    );
  }
}
