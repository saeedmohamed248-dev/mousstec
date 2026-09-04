import 'package:flutter/material.dart';

/// نوع الحقل في نماذج CRUD العامة.
enum FieldType { text, multiline, integer, decimal, boolean, date, fk, choice }

/// خيار لقائمة منسدلة ثابتة.
class Choice {
  const Choice(this.value, this.label);
  final String value;
  final String label;
}

/// تعريف حقل واحد — يُبنى منه عنصر الإدخال في النموذج.
class FieldSpec {
  const FieldSpec(
    this.key,
    this.label, {
    this.type = FieldType.text,
    this.required = false,
    this.fkEndpoint,
    this.fkLabelKey = 'name',
    this.choices = const [],
    this.keyboardIcon,
  });

  final String key;
  final String label;
  final FieldType type;
  final bool required;

  /// لحقول العلاقة (fk): مسار المورد المرتبط ومفتاح النص الظاهر.
  final String? fkEndpoint;
  final String fkLabelKey;

  /// لحقول الاختيار (choice).
  final List<Choice> choices;

  final IconData? keyboardIcon;
}
