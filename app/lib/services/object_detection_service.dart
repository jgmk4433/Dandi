import 'dart:typed_data';

import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart' show rootBundle;
import 'package:image/image.dart' as img;
import 'package:tflite_flutter/tflite_flutter.dart';

// 객체 검출 통과 여부와 판정 정보를 전달한다.
class DetectionResult {
  final bool passed;
  final String? label;
  final double? confidence;
  final double? iou;

  const DetectionResult({
    required this.passed,
    this.label,
    this.confidence,
    this.iou,
  });
}

// 검출 객체의 바운딩 박스와 신뢰도를 표현한다.
class _Box {
  final double x1, y1, x2, y2;
  final double confidence;

  const _Box(this.x1, this.y1, this.x2, this.y2, this.confidence);

  double get area {
    final double w = (x2 - x1) > 0 ? (x2 - x1) : 0.0;
    final double h = (y2 - y1) > 0 ? (y2 - y1) : 0.0;
    return w * h;
  }
}

// 두 바운딩 박스의 IoU 겹침 비율을 계산한다.
double _calculateIoU(_Box a, _Box b) {
  final interX1 = a.x1 > b.x1 ? a.x1 : b.x1;
  final interY1 = a.y1 > b.y1 ? a.y1 : b.y1;
  final interX2 = a.x2 < b.x2 ? a.x2 : b.x2;
  final interY2 = a.y2 < b.y2 ? a.y2 : b.y2;

  final interW = interX2 - interX1;
  final interH = interY2 - interY1;
  if (interW <= 0 || interH <= 0) return 0.0;

  final interArea = interW * interH;
  final unionArea = a.area + b.area - interArea;
  if (unionArea <= 0) return 0.0;

  return interArea / unionArea;
}

class ObjectDetectionService {
  Interpreter? _interpreter;
  List<String> _labels = [];

  static const String _modelPath = 'assets/models/model.tflite';
  static const String _labelsPath = 'assets/models/labels.txt';

  // COCO 테스트 라벨과 운영 라벨의 사용 모드를 전환한다.
  // 운영(커스텀 2클래스 모델) 기준이므로 false를 유지한다.
  static const bool kCocoTestMode = false;

  // 운영 모델의 클래스 인덱스: 0=person, 1=escooter
  static const List<String> _requiredLabelsReal = ['person', 'escooter'];
  static const List<String> _requiredLabelsCocoTest = ['person', 'bicycle'];

  List<String> get _requiredLabels =>
      kCocoTestMode ? _requiredLabelsCocoTest : _requiredLabelsReal;

  // 모드별 객체 검출 신뢰도 기준을 적용한다.
  static const double _confidenceThreshold = kCocoTestMode ? 0.3 : 0.5;

  // 사람과 이동수단의 탑승 여부를 판단할 IoU 기준이다.
  static const double _ridingIoUThreshold = 0.1;

  // end-to-end 출력의 바운딩 박스 좌표 해석 방식을 지정한다.
  // 'auto'는 값의 대소 관계로 추정하므로 가장자리 객체에서 오판할 수 있다.
  // 모델 출력 형식이 확정되면 'xyxy' 또는 'cxcywh'로 고정한다.
  static const String kBoxFormat = 'auto';

  bool get isModelLoaded => _interpreter != null;

  // 라벨 파일이 현재 모드의 필수 라벨을 모두 포함하는지 확인한다.
  List<String> get _labelsMissingFromFile => _requiredLabels
      .where((l) => !_labels.map((e) => e.toLowerCase()).contains(l))
      .toList();

  // 테스트 모드에서만 오토바이를 이륜차 라벨로 통합해 정규화한다.
  String _normalizeLabel(String raw) {
    final label = raw.toLowerCase();
    if (kCocoTestMode && label == 'motorcycle') return 'bicycle';
    return label;
  }

  // TFLite 모델과 라벨을 로드하고 입출력 정보를 확인한다.
  Future<void> loadModel() async {
    try {
      _interpreter = await Interpreter.fromAsset(_modelPath);
      _labels = await _loadLabels(_labelsPath);

      // 로드된 모델의 텐서와 판정 라벨 정보를 기록한다.
      final it = _interpreter!.getInputTensor(0);
      final ot = _interpreter!.getOutputTensor(0);
      debugPrint('=== TFLite 모델 정보 ===');
      debugPrint('입력  shape: ${it.shape}, type: ${it.type}');
      debugPrint('출력  shape: ${ot.shape}, type: ${ot.type}');
      debugPrint('라벨: $_labels (${_labels.length}개)');
      debugPrint('판정 대상: $_requiredLabels'
          '${kCocoTestMode ? "  ⚠️ COCO 테스트 모드 (운영 전 kCocoTestMode=false 로 변경)" : ""}');

      // 라벨 파일과 판정 대상이 어긋나면 모든 사진이 조용히 미전송된다.
      if (_labels.isEmpty) {
        debugPrint('❌ 치명적: $_labelsPath 를 읽지 못했습니다. '
            'pubspec.yaml 의 assets 등록을 확인하세요.');
      } else {
        final missing = _labelsMissingFromFile;
        if (missing.isNotEmpty) {
          debugPrint('❌ 치명적: 라벨 파일에 필수 라벨이 없습니다 -> ${missing.join(', ')}');
          debugPrint('   현재 라벨 파일: $_labels');
          debugPrint('   $_labelsPath 를 아래 2줄(클래스 인덱스 순서)로 교체하세요:');
          debugPrint('     person');
          debugPrint('     escooter');
          debugPrint('   이 상태로는 판정이 항상 실패해 서버 전송이 발생하지 않습니다.');
        } else if (!kCocoTestMode && _labels.length != _requiredLabelsReal.length) {
          debugPrint('⚠️ 경고: 운영 모드인데 라벨이 ${_labels.length}개입니다. '
              'COCO 라벨 파일이 남아있는지 확인하세요.');
        }
      }
      debugPrint('======================');
    } catch (e) {
      debugPrint('tflite 모델 로드 실패: $e');
      _interpreter = null;
    }
  }

  // 에셋 파일에서 빈 줄을 제외한 라벨 목록을 읽는다.
  Future<List<String>> _loadLabels(String path) async {
    try {
      final raw = await rootBundle.loadString(path);
      return raw
          .split('\n')
          .map((line) => line.trim())
          .where((line) => line.isNotEmpty)
          .toList();
    } catch (e) {
      return [];
    }
  }

  // 정수 값을 지정 범위로 제한한다.
  int _clampInt(int v, int lo, int hi) {
    if (v < lo) return lo;
    if (v > hi) return hi;
    return v;
  }

  // 기기 내 추론으로 사람과 이동수단의 탑승 여부를 판정한다.
  Future<DetectionResult> detect(img.Image image) async {
    if (_interpreter == null) {
      throw StateError('모델이 로드되지 않았습니다.');
    }

    // 라벨이 어긋난 상태의 추론은 결과가 무의미하므로 원인을 명시해 중단한다.
    final labelIssue = _labelsMissingFromFile;
    if (labelIssue.isNotEmpty) {
      throw StateError('라벨 설정 오류: $_labelsPath 에 ${labelIssue.join(', ')} '
          '라벨이 없습니다. (현재 라벨: $_labels)');
    }

    final inputTensor = _interpreter!.getInputTensor(0);
    final outputTensor = _interpreter!.getOutputTensor(0);
    final List<int> inputShape = inputTensor.shape;
    final List<int> outputShape = outputTensor.shape;
    final inputType = inputTensor.type;
    final outputType = outputTensor.type;

    debugPrint('[detect] 입력 shape: $inputShape, type: $inputType');
    debugPrint('[detect] 출력 shape: $outputShape, type: $outputType');

    // 모델 텐서 구조에서 입력 크기와 채널 순서를 결정한다.
    final bool isNCHW = inputShape.length == 4 && inputShape[1] == 3;
    final int inH = isNCHW ? inputShape[2] : inputShape[1];
    final int inW = isNCHW ? inputShape[3] : inputShape[2];

    debugPrint('[detect] 모델 입력 크기: ${inW}x$inH (${isNCHW ? "NCHW" : "NHWC"})');

    // 원본 비율을 유지한 letterbox 방식으로 모델 입력 이미지를 구성한다.
    final double scale = (inW / image.width) < (inH / image.height)
        ? inW / image.width
        : inH / image.height;
    final int newW = (image.width * scale).round();
    final int newH = (image.height * scale).round();
    final int padX = ((inW - newW) / 2).round();
    final int padY = ((inH - newH) / 2).round();

    final shrunk = img.copyResize(image, width: newW, height: newH);

    // 회색 여백 캔버스 중앙에 축소 이미지를 배치한다.
    final resized = img.Image(width: inW, height: inH);
    img.fill(resized, color: img.ColorRgb8(114, 114, 114));
    img.compositeImage(resized, shrunk, dstX: padX, dstY: padY);

    debugPrint('[detect] 레터박스: 원본 ${image.width}x${image.height} '
        '-> ${newW}x$newH (여백 ${padX},${padY})');

    // 입력 텐서 타입에 맞는 고정 길이 평탄 버퍼를 생성한다.
    final int inputElements = inputShape.reduce((a, b) => a * b);
    final inQ = inputTensor.params;

    // NCHW 또는 NHWC 채널 순서에 맞는 버퍼 인덱스를 계산한다.
    int flatIndex(int c, int y, int x) {
      if (isNCHW) {
        return c * (inH * inW) + y * inW + x;
      }
      return (y * inW + x) * 3 + c;
    }

    Object input;
    if (inputType == TensorType.uint8) {
      final buf = Uint8List(inputElements);
      for (int y = 0; y < inH; y++) {
        for (int x = 0; x < inW; x++) {
          final p = resized.getPixel(x, y);
          buf[flatIndex(0, y, x)] = p.r.round();
          buf[flatIndex(1, y, x)] = p.g.round();
          buf[flatIndex(2, y, x)] = p.b.round();
        }
      }
      input = buf.reshape(inputShape);
    } else if (inputType == TensorType.int8) {
      final buf = Int8List(inputElements);
      final double scale = inQ.scale != 0 ? inQ.scale : (1.0 / 255.0);
      final int zeroPoint = inQ.zeroPoint;
      int q(num raw) =>
          _clampInt(((raw / 255.0) / scale + zeroPoint).round(), -128, 127);
      for (int y = 0; y < inH; y++) {
        for (int x = 0; x < inW; x++) {
          final p = resized.getPixel(x, y);
          buf[flatIndex(0, y, x)] = q(p.r);
          buf[flatIndex(1, y, x)] = q(p.g);
          buf[flatIndex(2, y, x)] = q(p.b);
        }
      }
      input = buf.reshape(inputShape);
    } else {
      // float32 입력은 0~1 범위로 정규화한다.
      final buf = Float32List(inputElements);
      for (int y = 0; y < inH; y++) {
        for (int x = 0; x < inW; x++) {
          final p = resized.getPixel(x, y);
          buf[flatIndex(0, y, x)] = p.r / 255.0;
          buf[flatIndex(1, y, x)] = p.g / 255.0;
          buf[flatIndex(2, y, x)] = p.b / 255.0;
        }
      }
      input = buf.reshape(inputShape);
    }

    // 모델 출력 텐서의 타입과 크기에 맞는 버퍼를 생성한다.
    final int outputElements = outputShape.reduce((a, b) => a * b);
    final bool outputIsQuantized =
        outputType == TensorType.uint8 || outputType == TensorType.int8;

    Object output;
    if (outputType == TensorType.uint8) {
      output = Uint8List(outputElements).reshape(outputShape);
    } else if (outputType == TensorType.int8) {
      output = Int8List(outputElements).reshape(outputShape);
    } else {
      output = Float32List(outputElements).reshape(outputShape);
    }

    // 전처리된 입력으로 TFLite 추론을 실행한다.
    try {
      _interpreter!.run(input, output);
    } catch (e) {
      throw StateError('추론 실패: $e '
          '[입력 shape=$inputShape type=$inputType 크기=${inW}x$inH / '
          '출력 shape=$outputShape type=$outputType]');
    }

    // 출력 shape로 end-to-end 및 기존 YOLO 형식을 구분한다.
    final bool isEndToEnd = outputShape.length == 3 && outputShape[2] == 6;

    final outQ = outputTensor.params;
    final nested = output as List;

    double deq(Object? raw) {
      final v = (raw as num).toDouble();
      if (outputIsQuantized) {
        final double scale = outQ.scale != 0 ? outQ.scale : 1.0;
        return (v - outQ.zeroPoint) * scale;
      }
      return v;
    }

    final Map<String, List<_Box>> boxesByLabel = {};
    double highestConfidence = 0.0;

    if (isEndToEnd) {
      // end-to-end 출력의 탐지 행을 순회한다.
      final int detCount = outputShape[1];
      debugPrint('[detect] 출력형식: YOLO26 end-to-end (최대 $detCount개, xyxy)');

      // 낮은 신뢰도 검출도 진단 로그용으로 수집한다.
      final List<String> rawLog = [];
      int coordLogged = 0;

      for (int i = 0; i < detCount; i++) {
        final row = nested[0][i] as List;

        final double confidence = deq(row[4]);
        final int classId = deq(row[5]).round();

        // 판정과 별도로 기준 이상의 원시 검출을 기록한다.
        if (confidence >= 0.15 && classId >= 0 && classId < _labels.length) {
          rawLog.add('${_labels[classId]}:${confidence.toStringAsFixed(2)}');
        }

        if (confidence <= _confidenceThreshold) continue;
        if (classId < 0 || classId >= _labels.length) continue;

        if (confidence > highestConfidence) highestConfidence = confidence;

        final double a = deq(row[0]);
        final double b = deq(row[1]);
        final double c = deq(row[2]);
        final double d = deq(row[3]);

        // 일부 원시 좌표를 진단 로그로 기록한다.
        if (coordLogged < 4) {
          debugPrint('[detect] 좌표원본 ${_labels[classId]}: '
              '${a.toStringAsFixed(1)}, ${b.toStringAsFixed(1)}, '
              '${c.toStringAsFixed(1)}, ${d.toStringAsFixed(1)}');
          coordLogged++;
        }

        // 설정된 좌표 형식에 따라 xyxy 또는 cxcywh로 해석한다.
        double x1, y1, x2, y2;
        final bool treatAsXyxy = kBoxFormat == 'xyxy'
            ? true
            : kBoxFormat == 'cxcywh'
                ? false
                : (c > a && d > b);
        if (treatAsXyxy) {
          // xyxy 좌표를 그대로 사용한다.
          x1 = a;
          y1 = b;
          x2 = c;
          y2 = d;
        } else {
          // cxcywh 좌표를 xyxy로 변환한다.
          x1 = a - c / 2;
          y1 = b - d / 2;
          x2 = a + c / 2;
          y2 = b + d / 2;
        }

        final label = _normalizeLabel(_labels[classId]);

        boxesByLabel
            .putIfAbsent(label, () => [])
            .add(_Box(x1, y1, x2, y2, confidence));
      }

      debugPrint('[detect] 원본 검출(0.15↑): ${rawLog.isEmpty ? "없음" : rawLog.join(", ")}');
    } else {
      // 기존 YOLO 출력의 클래스별 신뢰도와 박스를 해석한다.
      final int dim1 = outputShape[1];
      final int dim2 = outputShape[2];
      final bool channelsFirst = dim1 <= dim2;
      final int channelDim = channelsFirst ? dim1 : dim2;
      final int boxCount = channelsFirst ? dim2 : dim1;
      final int actualNumClasses = channelDim - 4;

      debugPrint('[detect] 출력형식: 기존 YOLO (채널수=$channelDim, '
          '박스수=$boxCount, 클래스수=$actualNumClasses)');

      if (actualNumClasses != _labels.length) {
        debugPrint('⚠️ 경고: 모델 출력 클래스 수($actualNumClasses)와 '
            'labels.txt 클래스 수(${_labels.length})가 다릅니다.');
      }

      double outputAt(int channel, int box) =>
          deq(channelsFirst ? nested[0][channel][box] : nested[0][box][channel]);

      for (int b = 0; b < boxCount; b++) {
        final double cx = outputAt(0, b);
        final double cy = outputAt(1, b);
        final double w = outputAt(2, b);
        final double h = outputAt(3, b);

        for (int c = 0; c < actualNumClasses; c++) {
          final double confidence = outputAt(4 + c, b);
          if (confidence > _confidenceThreshold) {
            if (confidence > highestConfidence) highestConfidence = confidence;
            if (c >= _labels.length) continue;

            final label = _normalizeLabel(_labels[c]);
            boxesByLabel.putIfAbsent(label, () => []).add(
                  _Box(cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2, confidence),
                );
          }
        }
      }
    }

    // IoU 연산량을 제한하기 위해 라벨별 상위 박스만 유지한다.
    const int maxBoxesPerLabel = 20;
    for (final label in boxesByLabel.keys) {
      final boxes = boxesByLabel[label]!;
      if (boxes.length > maxBoxesPerLabel) {
        boxes.sort((a, b) => b.confidence.compareTo(a.confidence));
        boxesByLabel[label] = boxes.sublist(0, maxBoxesPerLabel);
      }
    }

    final Set<String> detectedLabels = boxesByLabel.keys.toSet();
    debugPrint('[detect] 검출된 라벨: $detectedLabels');

    final missing =
        _requiredLabels.where((l) => !detectedLabels.contains(l)).toList();
    final bool hasAllRequiredLabels = missing.isEmpty;

    // 필수 라벨 박스 조합 중 최대 IoU를 계산한다.
    double bestIoU = 0.0;
    if (hasAllRequiredLabels) {
      // 현재 모드의 필수 라벨을 기준으로 비교 대상을 선택한다.
      final boxesA = boxesByLabel[_requiredLabels[0]] ?? const <_Box>[];
      final boxesB = boxesByLabel[_requiredLabels[1]] ?? const <_Box>[];

      debugPrint('[detect] 겹침 계산: ${_requiredLabels[0]}(${boxesA.length}개) '
          'x ${_requiredLabels[1]}(${boxesB.length}개)');

      for (final p in boxesA) {
        for (final s in boxesB) {
          final iou = _calculateIoU(p, s);
          if (iou > bestIoU) bestIoU = iou;
        }
      }
    }

    final bool isRiding = hasAllRequiredLabels && bestIoU >= _ridingIoUThreshold;
    debugPrint('[detect] 최대 IoU=$bestIoU, 탑승판정=$isRiding');

    String resultLabel;
    if (!hasAllRequiredLabels) {
      resultLabel = '인식 실패 (미검출: ${missing.join(', ')})';
    } else if (isRiding) {
      resultLabel = '탑승 중 (겹침 ${(bestIoU * 100).toStringAsFixed(1)}%)';
    } else {
      resultLabel = '${_requiredLabels.join(' / ')} 각각 검출됐으나 겹치지 않음 '
          '(겹침 ${(bestIoU * 100).toStringAsFixed(1)}%, '
          '기준 ${(_ridingIoUThreshold * 100).toStringAsFixed(0)}% 미만)';
    }

    return DetectionResult(
      passed: isRiding,
      label: resultLabel,
      confidence: highestConfidence,
      iou: bestIoU,
    );
  }

  // TFLite 인터프리터 자원을 해제한다.
  void dispose() {
    _interpreter?.close();
  }
}
