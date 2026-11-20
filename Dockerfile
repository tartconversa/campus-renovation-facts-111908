FROM python:3.14-slim@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f
WORKDIR /work
COPY . /work
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUTF8=1
# 默认：成形一期事实仓并完成四边界守恒验证；state/ 在 .gitignore 中，仅作容器内演示
CMD ["python", "-I", "fw.py", "build", "--release", "R001", "--state", "/tmp/fw-state"]
# 可选：
#   自动化测试   docker run --rm --network none <image> python -I -m unittest discover -s tests -v
#   来源自检     docker run --rm --network none <image> python -I support/check_inputs.py
#   钻取查询     docker run --rm --network none <image> python -I fw.py overview --state /tmp/fw-state --release R001
