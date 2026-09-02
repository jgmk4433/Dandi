import 'dart:io';
import 'package:flutter/material.dart';
import 'package:camera/camera.dart';
import 'package:image/image.dart' as img;
import 'package:path_provider/path_provider.dart';

import '../services/api_service.dart';
import '../services/object_detection_service.dart';
import '../services/local_storage_service.dart';
import '../services/location_service.dart';
import '../utils/image_utils.dart';

const bool kEnableTfliteDetection = true; // TFLite 사전 검수 활성화 여부

// 검출 입력에만 중앙 크롭을 적용하고 증거 이미지는 전체 프레임으로 유지한다.
const bool kCropToGuideBox = false;

// 피사체 누락을 방지하는 검출용 중앙 크롭 비율이다.
const double kCropWidthFactor = 0.75;
const double kCropHeightFactor = 0.80;

// 오프라인 증거 이미지의 저장 여부와 압축 기준을 정의한다.
const bool kSaveOfflineImage = false;
const int kOfflineImageMaxSize = 1080;
const int kOfflineImageQuality = 70;

class CameraScreen extends StatefulWidget {
  const CameraScreen({Key? key}) : super(key: key);
  @override
  State<CameraScreen> createState() => _CameraScreenState();
}

class _CameraScreenState extends State<CameraScreen> {
  final ApiService _apiService = ApiService();
  final ObjectDetectionService _detectionService = ObjectDetectionService();
  CameraController? _controller;

  bool _isCameraInitialized = false;
  File? _reviewImageFile;
  DateTime? _captureTimestamp;
  bool _isProcessing = false;
  bool _isUploading = false;
  String _processingLabel = '';

  // 객체 검출 실패 사유를 사용자에게 표시한다.
  String? _detectionFailMessage;

  // 촬영 시점의 좌표와 주소를 보관한다.
  LocationResult? _capturedLocation;

  @override
  void initState() {
    super.initState();
    _initCamera();
    if (kEnableTfliteDetection) {
      _detectionService.loadModel(); 
    }
  }

  // 첫 번째 카메라를 고해상도 촬영 모드로 초기화한다.
  Future<void> _initCamera() async {
    try {
      final cameras = await availableCameras();
      if (cameras.isEmpty) return;
      
      _controller = CameraController(cameras.first, ResolutionPreset.high, enableAudio: false);
      await _controller!.initialize();
      if (mounted) {
        setState(() => _isCameraInitialized = true);
      }
    } catch (e) {
      debugPrint('카메라 초기화 에러: $e');
    }
  }

  @override
  void dispose() {
    _controller?.dispose();
    _detectionService.dispose();
    super.dispose();
  }

  // 사진 촬영 후 객체 검출, 위치 조회 및 타임스탬프 합성을 수행한다.
  Future<void> _takePicture() async {
    if (_controller == null || !_controller!.value.isInitialized) return;
    if (_isProcessing) return;

    setState(() {
      _isProcessing = true;
      _processingLabel = '사진 처리 중...';
      _detectionFailMessage = null;
    });

    try {
      final XFile picture = await _controller!.takePicture();
      _captureTimestamp = DateTime.now();

      // 이미지 처리와 위치 조회를 병렬로 시작한다.
      final locationFuture = LocationService.getCurrentLocationWithAddress();
      
      final bytes = await picture.readAsBytes();
      img.Image? decodedImage = img.decodeImage(bytes);

      // EXIF 방향 정보를 픽셀에 반영해 검출 방향을 보정한다.
      if (decodedImage != null) {
        decodedImage = img.bakeOrientation(decodedImage);
        debugPrint('[촬영] EXIF 회전 적용 후 크기: '
            '${decodedImage.width}x${decodedImage.height}');
      }
      
      if (decodedImage != null) {
        // 서버 전송 전에 사람과 킥보드 탑승 여부를 검수한다.
        if (kEnableTfliteDetection) {
          setState(() => _processingLabel = '사람/킥보드 인식 중...');

          if (!_detectionService.isModelLoaded) {
            setState(() => _detectionFailMessage = '신고 실패 (인식 모델 로드 안 됨)');
            return;
          }

          // 검출용 복사본만 중앙 크롭해 증거 원본을 보존한다.
          img.Image detectionInput = decodedImage;
          if (kCropToGuideBox) {
            final int cw = (decodedImage.width * kCropWidthFactor).round();
            final int ch = (decodedImage.height * kCropHeightFactor).round();
            final int cx = ((decodedImage.width - cw) / 2).round();
            final int cy = ((decodedImage.height - ch) / 2).round();
            detectionInput = img.copyCrop(decodedImage,
                x: cx, y: cy, width: cw, height: ch);
            debugPrint('[촬영] 검출용 크롭: '
                '${decodedImage.width}x${decodedImage.height} -> ${cw}x$ch');
          }

          final result = await _detectionService.detect(detectionInput);
          if (!result.passed) {
            // 탑승 조건 미충족 시 전송을 중단하고 실패 사유를 표시한다.
            setState(() => _detectionFailMessage = '신고 실패 (${result.label ?? "사람+킥보드 탑승 인식 안 됨"})');
            return;
          }
        }

        // 촬영 위치 조회 결과를 확보한다.
        setState(() => _processingLabel = '위치 정보 확인 중...');
        _capturedLocation = await locationFuture;
        debugPrint(_capturedLocation == null
            ? '[촬영] 위치 정보 없음 (권한/GPS/타임아웃 - 위 [위치] 로그 참고)'
            : '[촬영] 위치 확보: ${_capturedLocation!.address}');

        // 촬영 시각을 이미지 픽셀에 합성한다.
        decodedImage = await ImageUtils.burnTimestamp(decodedImage, _captureTimestamp!);
        
        // 검수용 이미지를 임시 JPG 파일로 저장한다.
        final directory = await getTemporaryDirectory();
        final timestampFile = File('${directory.path}/capture_${DateTime.now().millisecondsSinceEpoch}.jpg');
        await timestampFile.writeAsBytes(img.encodeJpg(decodedImage, quality: 85));
        
        setState(() {
          _reviewImageFile = timestampFile;
        });
      }
    } catch (e) {
      debugPrint('촬영 에러: $e');
      // 처리 오류의 상세 원인을 화면에 표시한다.
      setState(() => _detectionFailMessage = '신고 실패 (처리 중 오류: $e)');
    } finally {
      if (mounted) {
        setState(() => _isProcessing = false);
      }
      // 검출 실패 메시지를 일정 시간 후 자동으로 숨긴다.
      if (_detectionFailMessage != null) {
        Future.delayed(const Duration(seconds: 3), () {
          if (mounted && _reviewImageFile == null) {
            setState(() => _detectionFailMessage = null);
          }
        });
      }
    }
  }

  // 촬영 결과와 위치 정보를 초기화해 재촬영 상태로 전환한다.
  void _retake() {
    setState(() {
      _reviewImageFile = null;
      _captureTimestamp = null;
      _capturedLocation = null;
    });
  }

  // 신고 이미지를 중앙 허브로 전송하고 결과를 로컬 기록에 저장한다.
  Future<void> _uploadReviewImage() async {
    if (_reviewImageFile == null || _isUploading) return;

    setState(() => _isUploading = true);

    // 전송 결과와 무관하게 사용할 로컬 추적 ID를 생성한다.
    final localId = 'local_${DateTime.now().millisecondsSinceEpoch}';

    try {
      final regionCode = await LocalStorageService.getRegionCode();

      final result = await _apiService.submitEnforcement(
        _reviewImageFile!,
        _captureTimestamp!,
        regionCode: regionCode,
        address: _capturedLocation?.address,
        latitude: _capturedLocation?.latitude,
        longitude: _capturedLocation?.longitude,
      );

      await _saveReport(
        localId: localId,
        traceId: result.traceId,
        // ACCEPTED 응답을 사용자용 상태로 변환한다.
        status: result.status == 'ACCEPTED' ? '수신 완료' : result.status,
      );

      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text('수신 완료 - 중앙 허브가 접수했습니다.')),
        );
        _retake();
      }
    } catch (e) {
      // 전송 실패 신고를 재전송 가능한 로컬 기록으로 보관한다.
      debugPrint('허브 전송 실패, 로컬 보관으로 전환: $e');
      try {
        await _saveReport(
          localId: localId,
          traceId: null,
          status: '전송 대기 (허브 미연결)',
        );
        if (mounted) {
          ScaffoldMessenger.of(context).showSnackBar(
            const SnackBar(content: Text('허브 연결 불가 - 신고 내역에 보관했습니다.')),
          );
          _retake();
        }
      } catch (saveError) {
        if (mounted) {
          ScaffoldMessenger.of(context)
              .showSnackBar(SnackBar(content: Text('저장 실패: $saveError')));
        }
      }
    } finally {
      if (mounted) {
        setState(() => _isUploading = false);
      }
    }
  }

  // 전송 상태와 선택적으로 압축한 이미지를 로컬 신고 내역에 저장한다.
  Future<void> _saveReport({
    required String localId,
    String? traceId,
    required String status,
  }) async {
    String? savedImagePath;

    if (kSaveOfflineImage) {
      final documentsDir = await getApplicationDocumentsDirectory();
      savedImagePath = '${documentsDir.path}/report_$localId.jpg';

      final originalBytes = await _reviewImageFile!.readAsBytes();
      final decoded = img.decodeImage(originalBytes);

      if (decoded != null) {
        final int longSide =
            decoded.width > decoded.height ? decoded.width : decoded.height;
        final img.Image toSave = longSide > kOfflineImageMaxSize
            ? img.copyResize(
                decoded,
                width: decoded.width >= decoded.height ? kOfflineImageMaxSize : null,
                height: decoded.height > decoded.width ? kOfflineImageMaxSize : null,
              )
            : decoded;

        final jpg = img.encodeJpg(toSave, quality: kOfflineImageQuality);
        await File(savedImagePath).writeAsBytes(jpg);
        debugPrint('[저장] 사진: ${originalBytes.length ~/ 1024}KB '
            '-> ${jpg.length ~/ 1024}KB (${toSave.width}x${toSave.height})');
      } else {
        await _reviewImageFile!.copy(savedImagePath);
      }
    } else {
      // 사진 미보관 시 촬영 임시 파일을 삭제한다.
      try {
        if (await _reviewImageFile!.exists()) {
          final sizeKb = (await _reviewImageFile!.length()) ~/ 1024;
          await _reviewImageFile!.delete();
          debugPrint('[저장] 사진 미보관 모드 - 임시 파일 삭제 (${sizeKb}KB)');
        }
      } catch (e) {
        debugPrint('[저장] 임시 파일 삭제 실패(무시): $e');
      }
    }

    await LocalStorageService.addReport({
      'localId': localId,
      'traceId': traceId,
      'eventNo': null, // 허브 처리 완료 후 상태 조회로 채운다.
      'status': status,
      'capturedAt': _captureTimestamp!.toIso8601String(),
      'address': _capturedLocation?.address ?? '위치 정보 없음 (권한 또는 GPS 확인 필요)',
      'latitude': _capturedLocation?.latitude,
      'longitude': _capturedLocation?.longitude,
      'imagePath': savedImagePath,
    });
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: Text(_reviewImageFile == null ? 'PM 헬멧 신고 카메라' : '촬영 사진 검수')),
      body: _reviewImageFile != null ? _buildReviewView() : _buildCameraView(),
    );
  }

  // 촬영 가이드와 상태를 포함한 카메라 미리보기를 구성한다.
  Widget _buildCameraView() {
    if (!_isCameraInitialized || _controller == null) {
      return const Center(child: CircularProgressIndicator(color: Colors.lightGreen));
    }
    return Stack(
      children: [
        Positioned.fill(child: CameraPreview(_controller!)),

        // 사람과 킥보드를 함께 배치할 중앙 촬영 가이드를 표시한다.
        Align(
          alignment: Alignment.center,
          child: FractionallySizedBox(
            widthFactor: 0.55,
            heightFactor: 0.62,
            child: Container(
              decoration: BoxDecoration(
                border: Border.all(color: Colors.white, width: 2.5),
                borderRadius: BorderRadius.circular(4),
              ),
            ),
          ),
        ),
        Align(
          alignment: Alignment.topCenter,
          child: Padding(
            padding: const EdgeInsets.only(top: 24.0),
            child: Container(
              padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 8),
              decoration: BoxDecoration(
                color: Colors.black54,
                borderRadius: BorderRadius.circular(20),
              ),
              child: const Text(
                '킥보드/라이더 인식 중 (가이드에 맞춰주세요)',
                style: TextStyle(color: Colors.white, fontSize: 13),
              ),
            ),
          ),
        ),

        Align(
          alignment: Alignment.bottomCenter,
          child: Padding(
            padding: const EdgeInsets.only(bottom: 30.0),
            child: _isProcessing 
              ? Column(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    const CircularProgressIndicator(color: Colors.white),
                    const SizedBox(height: 10),
                    Text(_processingLabel, style: const TextStyle(color: Colors.white)),
                  ],
                )
              : Column(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    FloatingActionButton(
                      backgroundColor: Colors.white,
                      onPressed: _takePicture,
                      child: const Icon(Icons.camera_alt, color: Colors.black, size: 32),
                    ),
                    // 카메라 버튼 아래에 검출 실패 사유를 표시한다.
                    if (_detectionFailMessage != null) ...[
                      const SizedBox(height: 10),
                      Container(
                        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
                        decoration: BoxDecoration(
                          color: Colors.red.withOpacity(0.85),
                          borderRadius: BorderRadius.circular(16),
                        ),
                        child: Text(
                          _detectionFailMessage!,
                          style: const TextStyle(color: Colors.white, fontWeight: FontWeight.bold, fontSize: 13),
                        ),
                      ),
                    ],
                  ],
                ),
          ),
        )
      ],
    );
  }

  // 촬영 이미지, 위치 및 재촬영·전송 동작을 표시한다.
  Widget _buildReviewView() {
    return Column(
      children: [
        Expanded(
          child: Stack(
            children: [
              Positioned.fill(
                child: Image.file(_reviewImageFile!, width: double.infinity, fit: BoxFit.contain),
              ),
              // 촬영 위치를 이미지 위에 표시한다.
              Positioned(
                left: 12,
                right: 12,
                bottom: 12,
                child: Container(
                  padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
                  decoration: BoxDecoration(
                    color: Colors.black54,
                    borderRadius: BorderRadius.circular(8),
                  ),
                  child: Row(
                    children: [
                      const Icon(Icons.location_on, color: Colors.white, size: 18),
                      const SizedBox(width: 6),
                      Expanded(
                        child: Text(
                          _capturedLocation?.address ?? '위치 정보 확인 불가 (권한/GPS 확인 필요)',
                          style: const TextStyle(color: Colors.white, fontSize: 13),
                          overflow: TextOverflow.ellipsis,
                        ),
                      ),
                    ],
                  ),
                ),
              ),
            ],
          ),
        ),
        Padding(
          padding: const EdgeInsets.all(16.0),
          child: Row(
            children: [
              Expanded(
                child: OutlinedButton(
                  onPressed: _isUploading ? null : _retake,
                  style: OutlinedButton.styleFrom(
                    padding: const EdgeInsets.symmetric(vertical: 16),
                    side: const BorderSide(color: Colors.lightGreen),
                  ),
                  child: const Text('재촬영', style: TextStyle(color: Colors.lightGreen)),
                ),
              ),
              const SizedBox(width: 12),
              Expanded(
                child: ElevatedButton(
                  onPressed: _isUploading ? null : _uploadReviewImage,
                  style: ElevatedButton.styleFrom(
                    backgroundColor: Colors.lightGreen,
                    foregroundColor: Colors.white,
                    padding: const EdgeInsets.symmetric(vertical: 16),
                  ),
                  child: _isUploading
                      ? const SizedBox(width: 20, height: 20, child: CircularProgressIndicator(color: Colors.white, strokeWidth: 2))
                      : const Text('전송', style: TextStyle(fontWeight: FontWeight.bold)),
                ),
              ),
            ],
          ),
        ),
      ],
    );
  }
}
