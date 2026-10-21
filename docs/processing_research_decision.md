# Quyết định nghiên cứu Pain Point A

Cập nhật 21/10/2026. Mục tiêu là xử lý sau crawl ở quy mô lớn với chi phí và tỷ lệ loại nhầm đo được. Tài liệu này đối chiếu nghiên cứu gốc với số liệu riêng của project; các kết quả đo từ dữ liệu nội bộ ở `data/analysis/` không được commit.

## Điều đã được chứng minh tại máy

| Tầng | Bằng chứng | Quyết định hiện tại |
|---|---|---|
| Decode, kiểm file, SHA-256 | 92.244 dòng; cổng kỹ thuật 129,16 dòng/s hoặc 92,60 dòng/s dạng streaming; 761 dòng dùng lại asset trùng byte | Bật tự động kiểm tra và tái sử dụng asset, giữ mọi liên kết entity |
| dHash nghi trùng | 100 cặp người gắn nhãn; dHash gần không bảo đảm cùng ảnh | Chỉ sinh hàng duyệt |
| Xác nhận bằng NCC/SSIM | Tập test 60 cặp: NCC đúng 15/29 cặp cùng ảnh, gắn nhầm 1/31 cặp khác; 18,56 cặp/s | Chỉ ưu tiên duyệt, không auto-gộp |
| CLIP ảnh so tên entity | Balanced accuracy 48,25% trên test 497 ảnh | Không dùng để tự loại |
| NSFW model | Shadow 10.000 ảnh, 20,80 ảnh/s; thiếu nhãn dương độc lập | Chỉ đo điểm và thông lượng |

Những phép đo này thuộc các tầng khác nhau; không cộng thông lượng hay coi chúng là phép đo end-to-end. Corpus 89.201 ảnh được lấy từ ảnh con người đã chọn, nên không thể dùng tỷ lệ ảnh được giữ trong corpus để chứng minh máy lọc đúng ảnh crawl thô.

## Đối chiếu phương pháp

1. **Chia tầng và giữ provenance.** Loại lỗi file và hash trùng byte trước các model đắt tiền; mỗi bước phát một lý do riêng và giữ nguồn ảnh, entity, phiên bản nhãn. Thiết kế này giảm số ảnh phải chạy model, nhưng số tiết kiệm thực tế cần đo trên phân bố crawl thô, không suy từ corpus đã giữ.
2. **Dùng hash nhận diện ứng viên, không tự xóa.** [PDQ của Meta](https://github.com/facebook/ThreatExchange/blob/main/pdq/README.md) là perceptual hash để tìm ảnh gần giống. Trên dữ liệu này, dHash và cả NCC sau bước ứng viên vẫn nhầm ảnh khác; gộp byte theo SHA-256 là quy tắc tự động an toàn hơn. [SSIM gốc](https://www.cns.nyu.edu/~lcv/pubs/makeAbs.php?loc=Wang03) đo chất lượng/độ giống cấu trúc khi hai ảnh được căn chỉnh; không phải nhãn “cùng một tấm ảnh”.
3. **Không chọn ngưỡng CLIP từ tên ngắn.** [LAION-400M](https://arxiv.org/abs/2111.02114) dùng điểm CLIP để lọc cặp ảnh–văn bản ở quy mô web, nhưng mục tiêu đó khác xác minh ảnh đúng một địa danh/entity cụ thể. Nghiên cứu [Filtering, Distillation, and Hard Negatives](https://openaccess.thecvf.com/content/CVPR2023/papers/Radenovic_Filtering_Distillation_and_Hard_Negatives_for_Vision-Language_Pre-Training_CVPR_2023_paper.pdf) báo cáo ngưỡng CLIP quá chặt có thể bỏ nhiều cặp hữu ích. Kết quả CLIP của project thấp, nên thử mô tả entity đầy đủ và negative gần giống chỉ sau khi có nhãn đúng/sai địa bàn đủ rõ.
4. **Watermark/chữ đè cần nhãn riêng.** [PP-OCRv6](https://www.paddleocr.ai/latest/en/version3.x/algorithm/PP-OCRv6/PP-OCRv6.html) là lựa chọn OCR để phát hiện vùng chữ; OCR tìm thấy chữ không đồng nghĩa ảnh xấu, vì ảnh biển hiệu, lễ hội, bản đồ hay hiện vật cũng có chữ hợp lệ. Đo riêng vị trí/diện tích chữ, watermark ở mép, và tỷ lệ loại nhầm trên ảnh người giữ trước khi quyết định tự loại.
5. **Nội dung nhạy cảm cần test có positive.** Model NSFW hiện chỉ chạy shadow trên ảnh con người đã giữ. 38/10.000 ảnh vượt điểm 0,5 là số gắn cờ, không phải precision, recall hay false-positive rate. Với nội dung chính trị/phản động, cần policy và nhãn khác; không suy từ model NSFW.

## Thử nghiệm tiếp theo có giá trị nhất

Tạo tập đánh giá **theo lý do**, lấy từ cả ảnh `keep` và ảnh `reject` của sheet đã thẩm định, chỉ dùng ảnh giải mã được. Mỗi dòng có ảnh, entity + mô tả, quyết định giữ/bỏ gốc, nguyên văn note, lý do cần xác minh và nguồn. Lấy mẫu phân tầng theo batch, entity, loại lỗi và điểm model; giữ một phần entity chưa xuất hiện ở tập hiệu chuẩn. Hai người đánh giá độc lập các trường hợp chọn để đo mức đồng thuận; bất đồng phải được xử lý trước khi dùng làm ground truth. Không dùng `x` từ sheet ứng viên làm nhãn ảnh xấu.

Bước chuẩn bị đầu tiên đã được tự động hóa bằng `python scripts/prepare_reason_validation.py`. Nó xuất hai phiếu chấm mù và một file provenance riêng dưới `data/analysis/reason_validation_pilot/` (đều bị loại khỏi Git). Trong 1.000 dòng pilot, 223 ca hợp lệ được lấy: 19 ca ghi chú watermark/chữ đè, 59 sai entity/địa bàn, 5 chất lượng thấp, 40 lý do bỏ khác và 100 ca giữ. Xác suất được chọn theo từng tầng nằm trong `sampling_report.json`; số 19 thay vì 20 là vì một ca watermark không có ảnh giải mã hợp lệ. Đây là **mẫu để xác nhận nhãn**, chưa phải kết quả đánh giá model. File cho người chấm không chứa quyết định giữ/bỏ, ghi chú gốc hay URL nguồn để giảm thiên lệch.

Đo precision/recall và tỷ lệ loại nhầm theo **từng** lỗi trên holdout; ghi cả coverage tự động, thông lượng, giờ máy và giá tài nguyên thực. Ngưỡng chọn trên calibration, khóa trước khi mở test. Khi thiếu nhãn dương hoặc cận tin cậy còn quá rộng, output phải là `needs_review`/shadow, không phải `reject`. Ở quy mô 10–100M, dùng các số đo theo tầng để lập khoảng chi phí có giả định rõ; chưa gọi ngoại suy là hiệu năng production.
