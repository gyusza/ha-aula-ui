ARG BUILD_FROM
FROM $BUILD_FROM

ENV LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /opt/aula-ui

COPY requirements.txt /opt/aula-ui/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /opt/aula-ui/requirements.txt

COPY aula_ui /opt/aula-ui/aula_ui
COPY icon.svg /opt/aula-ui/icon.svg
COPY vendor /opt/aula-ui/vendor
COPY THIRD_PARTY_NOTICES.md /opt/aula-ui/THIRD_PARTY_NOTICES.md
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
