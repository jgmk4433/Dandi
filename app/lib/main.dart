// main.dart
import 'package:flutter/material.dart';
import 'screens/UX_camera_screen.dart';
import 'screens/UX_report_history_screen.dart';
import 'screens/UX_appeal_screen.dart';
import 'screens/UX_settings_screen.dart';

// 앱 진입점
void main() {
  runApp(const PMHelmetApp());
}

// 애플리케이션 루트 위젯 및 테마 설정
class PMHelmetApp extends StatelessWidget {
  const PMHelmetApp({Key? key}) : super(key: key);

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'PM 신문고',
      theme: ThemeData(
        primarySwatch: Colors.lightGreen,
        primaryColor: Colors.lightGreen,
        scaffoldBackgroundColor: Colors.white,
        appBarTheme: const AppBarTheme(
          backgroundColor: Colors.lightGreen,
          foregroundColor: Colors.white,
          centerTitle: true,
        ),
        textTheme: const TextTheme(
          bodyLarge: TextStyle(color: Colors.black),
          bodyMedium: TextStyle(color: Colors.black),
        ),
      ),
      home: const MainNavigator(),
    );
  }
}

// 하단 네비게이션 제어 위젯
class MainNavigator extends StatefulWidget {
  const MainNavigator({Key? key}) : super(key: key);

  @override
  State<MainNavigator> createState() => _MainNavigatorState();
}

class _MainNavigatorState extends State<MainNavigator> {
  int _currentIndex = 0;

  // 네비게이션 대상 화면 목록
  final List<Widget> _screens = [
    const CameraScreen(),
    const ReportHistoryScreen(),
    const AppealScreen(),
    const SettingsScreen(),
  ];

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: _screens[_currentIndex],
      // 탭 전환 바텀 네비게이션 바 구성
      bottomNavigationBar: BottomNavigationBar(
        currentIndex: _currentIndex,
        onTap: (index) {
          setState(() {
            _currentIndex = index;
          });
        },
        type: BottomNavigationBarType.fixed,
        backgroundColor: Colors.white,
        selectedItemColor: Colors.lightGreen,
        unselectedItemColor: Colors.black54,
        items: const [
          BottomNavigationBarItem(icon: Icon(Icons.camera_alt), label: '카메라'),
          BottomNavigationBarItem(icon: Icon(Icons.list_alt), label: '신고 내역'),
          BottomNavigationBarItem(icon: Icon(Icons.gavel), label: '이의제기'),
          BottomNavigationBarItem(icon: Icon(Icons.settings), label: '설정'),
        ],
      ),
    );
  }
}