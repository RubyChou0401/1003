Attribute VB_Name = "LeaveQualified"
Option Explicit

'==================================================
' 特別休假資格篩選（依勞動基準法第38條）
'   6個月以上未滿1年 ：3 日
'   1年以上未滿2年   ：7 日
'   2年以上未滿3年   ：10 日
'   3年以上未滿5年   ：每年14 日
'   5年以上未滿10年  ：每年15 日
'   10年以上         ：每1年加給1日，最多30日（滿10年=16日，滿24年起=30日）
'==================================================
Public Sub FilterAndExtractLeaveQualified()

    Dim wsCategory As Worksheet, wsData As Worksheet, wsResult As Worksheet
    Dim titleDict As Object
    Dim dataArr As Variant, categoryArr As Variant, keepCols As Variant
    Dim resultArr() As Variant
    Dim lastRowCategory As Long, lastRowData As Long, lastResultRow As Long
    Dim i As Long, j As Long, resultCount As Long, invalidDateCount As Long
    Dim targetYear As Long, targetMonth As Long, leaveDays As Long
    Dim matchTitle As String, leaveDesc As String
    Dim sheetName As String, inputYM As String
    Dim hireDate As Date, resignDate As Date, targetLastDay As Date
    Dim qualificationDate As Date, completionDate As Date

    Dim oldCalculation As XlCalculation
    Dim oldScreenUpdating As Boolean
    Dim oldEnableEvents As Boolean
    Dim oldDisplayAlerts As Boolean
    Dim settingsSaved As Boolean

    On Error GoTo ErrorHandler

    '==================================================
    ' 1. 儲存並調整 Excel 設定
    '==================================================
    oldCalculation = Application.Calculation
    oldScreenUpdating = Application.ScreenUpdating
    oldEnableEvents = Application.EnableEvents
    oldDisplayAlerts = Application.DisplayAlerts
    settingsSaved = True

    Application.ScreenUpdating = False
    Application.EnableEvents = False
    Application.Calculation = xlCalculationManual

    '==================================================
    ' 2. 來源工作表
    '==================================================
    Set wsCategory = ThisWorkbook.Worksheets("類別")
    Set wsData = ThisWorkbook.Worksheets("員工基本資料轉出(其他)")

    lastRowCategory = wsCategory.Cells(wsCategory.Rows.Count, "A").End(xlUp).Row
    lastRowData = wsData.Cells(wsData.Rows.Count, "B").End(xlUp).Row

    If lastRowCategory < 2 Then
        MsgBox "「類別」工作表沒有職稱資料。", vbExclamation
        GoTo SafeExit
    End If

    If lastRowData < 2 Then
        MsgBox "「員工基本資料轉出(其他)」沒有員工資料。", vbExclamation
        GoTo SafeExit
    End If

    '==================================================
    ' 3. 輸入判斷年月
    '==================================================
    inputYM = Trim$(InputBox( _
        "請輸入目標判斷年月，格式 YYYYMM（例如：202501）：", _
        "特別休假資格計算"))

    If inputYM = "" Then GoTo SafeExit

    If Len(inputYM) <> 6 Or Not inputYM Like "######" Then
        MsgBox "格式錯誤，請輸入 6 位數 YYYYMM，例如 202501。", vbExclamation
        GoTo SafeExit
    End If

    targetYear = CLng(Left$(inputYM, 4))
    targetMonth = CLng(Right$(inputYM, 2))

    If targetYear < 1900 Or targetYear > 9998 Then
        MsgBox "年份格式錯誤。", vbExclamation
        GoTo SafeExit
    End If

    If targetMonth < 1 Or targetMonth > 12 Then
        MsgBox "月份必須介於 01 至 12。", vbExclamation
        GoTo SafeExit
    End If

    targetLastDay = DateSerial(targetYear, targetMonth + 1, 0)

    '==================================================
    ' 4. 建立職稱字典
    '==================================================
    Set titleDict = CreateObject("Scripting.Dictionary")
    titleDict.CompareMode = vbTextCompare

    categoryArr = wsCategory.Range("A2:A" & lastRowCategory).Value2

    For i = 1 To UBound(categoryArr, 1)
        If Not IsError(categoryArr(i, 1)) Then
            matchTitle = Trim$(CStr(categoryArr(i, 1)))
            If Len(matchTitle) > 0 Then titleDict(matchTitle) = True
        End If
    Next i

    '==================================================
    ' 5. 讀取來源資料 A:Q
    '    M：職稱  O：到職日期  P：離職日期
    '    保留來源欄位：B、C、F、H、J、M、N、O、P
    '==================================================
    dataArr = wsData.Range("A2:Q" & lastRowData).Value
    keepCols = Array(2, 3, 6, 8, 10, 13, 14, 15, 16)

    ReDim resultArr(1 To UBound(dataArr, 1), 1 To 16)

    '==================================================
    ' 6. 篩選符合特休資格的員工
    '==================================================
    For i = 1 To UBound(dataArr, 1)

        If IsError(dataArr(i, 13)) Then GoTo NextEmployee
        If Not titleDict.Exists(Trim$(CStr(dataArr(i, 13)))) Then GoTo NextEmployee

        '到職日：支援日期、序列值、YYYYMMDD、民國年(如 113/01/01、1130101)
        If Not TryParseDate(dataArr(i, 15), hireDate) Then
            invalidDateCount = invalidDateCount + 1
            GoTo NextEmployee
        End If

        If hireDate > targetLastDay Then GoTo NextEmployee

        '只列入目標月份新取得特休資格者
        If Not GetLeaveInfo(hireDate, targetYear, targetMonth, _
                            leaveDays, leaveDesc, qualificationDate, completionDate) Then
            GoTo NextEmployee
        End If

        '離職日：須晚於年資屆滿日(即離職日 >= 特休生效日)才算取得資格
        If Not IsEmpty(dataArr(i, 16)) And Len(Trim$(CStr(dataArr(i, 16)))) > 0 Then
            If Not TryParseDate(dataArr(i, 16), resignDate) Then
                invalidDateCount = invalidDateCount + 1
                GoTo NextEmployee
            End If
            If resignDate <= completionDate Then GoTo NextEmployee
        End If

        resultCount = resultCount + 1

        'A:I 原始保留欄位
        For j = LBound(keepCols) To UBound(keepCols)
            resultArr(resultCount, j + 1) = dataArr(i, keepCols(j))
        Next j

        resultArr(resultCount, 10) = leaveDesc                 'J：特休資格說明
        resultArr(resultCount, 11) = leaveDays                 'K：法定特休天數
        resultArr(resultCount, 12) = IIf(leaveDesc = "滿半年", 1040, 2088) 'L：法定工時比例
        'M：計算期間排班時數、N：時薪金額 先留白
        'O：未休假工資 稍後設定公式
        resultArr(resultCount, 16) = leaveDesc & "未休假工資"   'P：薪資檔備註（直接為值）

NextEmployee:
    Next i

    '==================================================
    ' 7. 重建結果工作表
    '==================================================
    sheetName = "結果_" & inputYM

    Application.DisplayAlerts = False
    DeleteSheetIfExists sheetName

    Set wsResult = ThisWorkbook.Worksheets.Add(After:=wsData)
    wsResult.Name = sheetName

    For j = LBound(keepCols) To UBound(keepCols)
        wsResult.Cells(1, j + 1).Value = wsData.Cells(1, keepCols(j)).Value
    Next j

    wsResult.Range("J1:P1").Value = Array( _
        "特休資格說明", "法定特休天數", "法定工時比例", _
        "計算期間排班時數", "時薪金額", "未休假工資", "薪資檔備註")

    '==================================================
    ' 8. 寫入資料（P 欄為值，O 欄為公式）
    '==================================================
    lastResultRow = resultCount + 1

    If resultCount > 0 Then
        wsResult.Range("A2").Resize(resultCount, 16).Value = resultArr

        'O欄：未休假工資 = M / L * N * K * 8，無條件進位
        wsResult.Range("O2:O" & lastResultRow).FormulaR1C1 = _
            "=IFERROR(ROUNDUP(RC[-2]/RC[-3]*RC[-1]*RC[-4]*8,0),"""")"
    End If

    '==================================================
    ' 9. 設定格式
    '==================================================
    With wsResult

        With .Range("A1:P1")
            .Font.Bold = True
            .Font.Color = RGB(255, 255, 255)
            .Interior.Color = RGB(31, 78, 121)
            .HorizontalAlignment = xlCenter
            .VerticalAlignment = xlCenter
            .WrapText = True
        End With

        .Columns("H:I").NumberFormat = "yyyy/m/d"
        .Columns("K").NumberFormat = "0"
        .Columns("L").NumberFormat = "#,##0"
        .Columns("M:N").NumberFormat = "#,##0.00"
        .Columns("O").NumberFormat = "#,##0"

        .Range("A1:P" & lastResultRow).AutoFilter
        .Columns("A:P").EntireColumn.AutoFit

        If .Columns("J").ColumnWidth > 20 Then .Columns("J").ColumnWidth = 20
        If .Columns("M").ColumnWidth > 22 Then .Columns("M").ColumnWidth = 22
        If .Columns("P").ColumnWidth > 24 Then .Columns("P").ColumnWidth = 24

        .Rows(1).RowHeight = 32
    End With

    '==================================================
    ' 10. 凍結 A、B 欄及第一列
    '==================================================
    wsResult.Activate
    With ActiveWindow
        .FreezePanes = False
        .SplitColumn = 2
        .SplitRow = 1
        .FreezePanes = True
    End With
    wsResult.Range("C2").Select

    '==================================================
    ' 11. 完成提示
    '==================================================
    MsgBox "執行完成！" & vbCrLf & _
           "判斷年月：" & inputYM & vbCrLf & _
           "符合資格人數：" & resultCount & vbCrLf & _
           "結果工作表：" & sheetName & _
           IIf(invalidDateCount > 0, _
               vbCrLf & vbCrLf & "注意：有 " & invalidDateCount & _
               " 筆資料的到職/離職日期無法辨識，已略過，請檢查來源資料。", ""), _
           IIf(invalidDateCount > 0, vbExclamation, vbInformation)

SafeExit:
    On Error Resume Next
    Application.DisplayAlerts = oldDisplayAlerts
    If settingsSaved Then
        Application.ScreenUpdating = oldScreenUpdating
        Application.EnableEvents = oldEnableEvents
        Application.Calculation = oldCalculation
    End If
    On Error GoTo 0
    Exit Sub

ErrorHandler:
    MsgBox "執行失敗！" & vbCrLf & _
           "錯誤代碼：" & Err.Number & vbCrLf & _
           "錯誤說明：" & Err.Description, vbCritical
    Resume SafeExit

End Sub

'==================================================
' 勞基法第38條：依「年資滿幾年」回傳特休天數（未滿1年請另計半年）
'==================================================
Private Function AnnualLeaveDays(ByVal fullYears As Long) As Long
    Select Case fullYears
        Case Is < 1:   AnnualLeaveDays = 0
        Case 1:        AnnualLeaveDays = 7
        Case 2:        AnnualLeaveDays = 10
        Case 3 To 4:   AnnualLeaveDays = 14
        Case 5 To 9:   AnnualLeaveDays = 15
        Case Else
            '10年以上：每年加1日，上限30日（滿10年=16日）
            AnnualLeaveDays = fullYears + 6
            If AnnualLeaveDays > 30 Then AnnualLeaveDays = 30
    End Select
End Function

'==================================================
' 判斷到職日於目標年月是否「新取得」特休資格
'   completionDate   ：年資屆滿日（到職日加6個月/N年的對應日）
'   qualificationDate：特休生效日 = 年資屆滿日的隔天（以此日所屬月份判斷）
'   離職者須晚於年資屆滿日離職(離職日 > 屆滿日)才取得資格
'==================================================
Private Function GetLeaveInfo(ByVal hireDate As Date, _
                              ByVal targetYear As Long, _
                              ByVal targetMonth As Long, _
                              ByRef leaveDays As Long, _
                              ByRef leaveDesc As String, _
                              ByRef qualificationDate As Date, _
                              ByRef completionDate As Date) As Boolean

    Dim n As Long, baseN As Long
    Dim d As Date

    leaveDays = 0
    leaveDesc = ""
    qualificationDate = 0
    completionDate = 0

    '滿6個月：3天（未滿1年）
    d = DateAdd("m", 6, hireDate)
    If IsTargetMonth(d + 1, targetYear, targetMonth) Then
        leaveDays = 3
        leaveDesc = "滿半年"
        completionDate = d
    Else
        '滿N年（N>=1）：生效日(屆滿日隔天)可能跨到下一年(如12/31到職)，故多試前一年
        baseN = targetYear - Year(hireDate)
        For n = baseN To baseN - 1 Step -1
            If n >= 1 Then
                d = DateAdd("yyyy", n, hireDate)
                If IsTargetMonth(d + 1, targetYear, targetMonth) Then
                    leaveDays = AnnualLeaveDays(n)
                    leaveDesc = "滿" & n & "年"
                    completionDate = d
                    Exit For
                End If
            End If
        Next n
    End If

    If leaveDesc <> "" Then qualificationDate = completionDate + 1
    GetLeaveInfo = (leaveDesc <> "")

End Function

Private Function IsTargetMonth(ByVal d As Date, _
                               ByVal y As Long, _
                               ByVal m As Long) As Boolean
    IsTargetMonth = (Year(d) = y And Month(d) = m)
End Function

'==================================================
' 日期解析：Date / Excel序列值 / YYYYMMDD / 民國年(YYYMMDD、113/1/1)
'==================================================
Private Function TryParseDate(ByVal v As Variant, ByRef result As Date) As Boolean
    Dim s As String, parts() As String
    Dim y As Long, m As Long, d As Long

    On Error GoTo Fail
    If IsError(v) Or IsEmpty(v) Then Exit Function

    If VarType(v) = vbDate Then
        result = Int(CDate(v))
        TryParseDate = True
        Exit Function
    End If

    'Excel 序列值（儲存格為數字但非日期格式），YYYYMMDD / 民國 YYYMMDD 另行處理
    If VarType(v) <> vbString Then
        If IsNumeric(v) Then
            If CDbl(v) >= 1 And CDbl(v) < 1000000 Then
                result = Int(CDate(CDbl(v)))
                TryParseDate = True
                Exit Function
            End If
        End If
    End If

    s = Trim$(CStr(v))
    If Len(s) = 0 Then Exit Function

    If Not s Like "*[!0-9]*" Then
        '純數字
        Select Case Len(s)
            Case 8: y = CLng(Left$(s, 4)): m = CLng(Mid$(s, 5, 2)): d = CLng(Right$(s, 2))
            Case 7: y = CLng(Left$(s, 3)) + 1911: m = CLng(Mid$(s, 4, 2)): d = CLng(Right$(s, 2))
            Case Else: Exit Function
        End Select
    Else
        s = Replace(Replace(Replace(s, "-", "/"), ".", "/"), "　", "")
        parts = Split(s, "/")
        If UBound(parts) <> 2 Then
            If IsDate(s) Then result = Int(CDate(s)): TryParseDate = True
            Exit Function
        End If
        y = CLng(Trim$(parts(0))): m = CLng(Trim$(parts(1))): d = CLng(Trim$(parts(2)))
        If y < 1911 Then y = y + 1911          '民國年
    End If

    If y < 1900 Or y > 9998 Or m < 1 Or m > 12 Or d < 1 Or d > 31 Then Exit Function
    result = DateSerial(y, m, d)
    '驗證未被自動進位（如 2/30）
    TryParseDate = (Year(result) = y And Month(result) = m And Day(result) = d)
    Exit Function
Fail:
    TryParseDate = False
End Function

Private Sub DeleteSheetIfExists(ByVal sheetName As String)
    Dim ws As Worksheet
    For Each ws In ThisWorkbook.Worksheets
        If ws.Name = sheetName Then
            ws.Delete
            Exit Sub
        End If
    Next ws
End Sub

'==================================================
' 自我測試：於 VBE 即時運算視窗執行 Test_LeaveRules，無錯誤即通過
'==================================================
Public Sub Test_LeaveRules()
    Dim dys As Long, desc As String, q As Date, c As Date

    '天數對照表
    Debug.Assert AnnualLeaveDays(1) = 7
    Debug.Assert AnnualLeaveDays(2) = 10
    Debug.Assert AnnualLeaveDays(3) = 14
    Debug.Assert AnnualLeaveDays(4) = 14
    Debug.Assert AnnualLeaveDays(5) = 15
    Debug.Assert AnnualLeaveDays(9) = 15
    Debug.Assert AnnualLeaveDays(10) = 16
    Debug.Assert AnnualLeaveDays(11) = 17
    Debug.Assert AnnualLeaveDays(23) = 29
    Debug.Assert AnnualLeaveDays(24) = 30
    Debug.Assert AnnualLeaveDays(40) = 30

    '2024/1/1 到職：滿半年日 7/1、特休 7/2 生效 → 2024/7 取得3天；2025/1 取得7天；2026/1 取得10天
    Debug.Assert GetLeaveInfo(DateSerial(2024, 1, 1), 2024, 7, dys, desc, q, c) And dys = 3
    Debug.Assert c = DateSerial(2024, 7, 1) And q = DateSerial(2024, 7, 2)
    Debug.Assert GetLeaveInfo(DateSerial(2024, 1, 1), 2025, 1, dys, desc, q, c) And dys = 7
    Debug.Assert GetLeaveInfo(DateSerial(2024, 1, 1), 2026, 1, dys, desc, q, c) And dys = 10
    Debug.Assert Not GetLeaveInfo(DateSerial(2024, 1, 1), 2024, 8, dys, desc, q, c)
    Debug.Assert GetLeaveInfo(DateSerial(2015, 3, 15), 2025, 3, dys, desc, q, c) And dys = 16

    '7/31 到職：1/31 滿半年，2/1 才有3天
    Debug.Assert GetLeaveInfo(DateSerial(2024, 7, 31), 2025, 2, dys, desc, q, c) And dys = 3
    Debug.Assert c = DateSerial(2025, 1, 31) And q = DateSerial(2025, 2, 1)
    Debug.Assert Not GetLeaveInfo(DateSerial(2024, 7, 31), 2025, 1, dys, desc, q, c)

    '8/31 到職：滿半年日落在2月底(2/28)，3/1 生效
    Debug.Assert GetLeaveInfo(DateSerial(2024, 8, 31), 2025, 3, dys, desc, q, c) And dys = 3

    '12/31 到職：滿1年日 2024/12/31，2025/1/1 生效 → 2025/1 取得7天
    Debug.Assert GetLeaveInfo(DateSerial(2023, 12, 31), 2025, 1, dys, desc, q, c) And dys = 7
    Debug.Assert Not GetLeaveInfo(DateSerial(2023, 12, 31), 2024, 12, dys, desc, q, c)

    '日期解析
    Dim t As Date
    Debug.Assert TryParseDate("1130101", t) And t = DateSerial(2024, 1, 1)
    Debug.Assert TryParseDate("20240229", t) And t = DateSerial(2024, 2, 29)
    Debug.Assert TryParseDate("113/2/29", t) And t = DateSerial(2024, 2, 29)
    Debug.Assert Not TryParseDate("2023/2/29", t)
    Debug.Print "Test_LeaveRules 通過"
End Sub
