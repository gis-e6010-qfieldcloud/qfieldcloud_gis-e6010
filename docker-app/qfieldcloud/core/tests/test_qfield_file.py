import logging
import os
import tempfile
import time

import psycopg2
import requests
from django.http.response import HttpResponseRedirect
from qfieldcloud.authentication.models import AuthToken
from qfieldcloud.core.geodb_utils import delete_db_and_role
from qfieldcloud.core.models import Geodb, Project, User
from rest_framework import status
from rest_framework.test import APITransactionTestCase

from .utils import testdata_path

logging.disable(logging.CRITICAL)


class QfcTestCase(APITransactionTestCase):
    def setUp(self):
        # Create a user
        self.user1 = User.objects.create_user(username="user1", password="abc123")

        self.user2 = User.objects.create_user(username="user2", password="abc123")

        self.token1 = AuthToken.objects.get_or_create(user=self.user1)[0]

        # Create a project
        self.project1 = Project.objects.create(
            name="project1", is_public=False, owner=self.user1
        )

        try:
            delete_db_and_role("test", self.user1.username)
        except Exception:
            pass

        self.geodb = Geodb.objects.create(
            user=self.user1,
            dbname="test",
            hostname="geodb",
            port=5432,
        )

        self.conn = psycopg2.connect(
            dbname="test",
            user=os.environ.get("GEODB_USER"),
            password=os.environ.get("GEODB_PASSWORD"),
            host="geodb",
            port=5432,
        )

    def tearDown(self):
        self.conn.close()

        # Remove all projects avoiding bulk delete in order to use
        # the overrided delete() function in the model
        for p in Project.objects.all():
            p.delete()

        User.objects.all().delete()
        # Remove credentials
        self.client.credentials()

    def test_list_files_for_qfield(self):
        self.client.credentials(HTTP_AUTHORIZATION="Token " + self.token1.key)

        cur = self.conn.cursor()

        cur.execute(
            """
            CREATE TABLE point (
                id          integer,
                geometry   geometry(point, 2056)
            );
            """
        )

        self.conn.commit()

        cur.execute(
            """
            INSERT INTO point(id, geometry)
            VALUES(1, ST_GeomFromText('POINT(2725505 1121435)', 2056));
            """
        )
        self.conn.commit()

        # Add the qgis project
        file = testdata_path("delta/project2.qgs")
        response = self.client.post(
            "/api/v1/files/{}/project.qgs/".format(self.project1.id),
            {"file": open(file, "rb")},
            format="multipart",
        )
        self.assertTrue(status.is_success(response.status_code))

        response = self.client.post(
            "/api/v1/qfield-files/export/{}/".format(self.project1.id)
        )
        self.assertTrue(status.is_success(response.status_code))

        # Wait for the worker to finish
        for _ in range(20):
            time.sleep(3)
            response = self.client.get(
                "/api/v1/qfield-files/export/{}/".format(self.project1.id),
            )
            payload = response.json()
            if payload["status"] == "STATUS_EXPORTED":
                response = self.client.get(
                    "/api/v1/qfield-files/{}/".format(self.project1.id),
                )
                json_resp = response.json()
                files = sorted(json_resp["files"], key=lambda k: k["name"])
                self.assertEqual(files[0]["name"], "data.gpkg")
                self.assertEqual(files[1]["name"], "project_qfield.qgs")
                return
            elif payload["status"] == "STATUS_ERROR":
                self.fail("Worker failed with error")

        self.fail("Worker didn't finish")

    def test_list_files_for_qfield_incomplete_project(self):
        # the qgs file is missing
        self.client.credentials(HTTP_AUTHORIZATION="Token " + self.token1.key)

        # Add files to the project
        file = testdata_path("delta/points.geojson")
        response = self.client.post(
            "/api/v1/files/{}/points.geojson/".format(self.project1.id),
            {"file": open(file, "rb")},
            format="multipart",
        )
        self.assertTrue(status.is_success(response.status_code))

        response = self.client.post(
            "/api/v1/qfield-files/export/{}/".format(self.project1.id)
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "no_qgis_project")

    def test_download_file_for_qfield(self):
        self.client.credentials(HTTP_AUTHORIZATION="Token " + self.token1.key)

        # Add files to the project
        file = testdata_path("delta/points.geojson")
        response = self.client.post(
            "/api/v1/files/{}/points.geojson/".format(self.project1.id),
            {"file": open(file, "rb")},
            format="multipart",
        )
        self.assertTrue(status.is_success(response.status_code))

        file = testdata_path("delta/polygons.geojson")
        response = self.client.post(
            "/api/v1/files/{}/polygons.geojson/".format(self.project1.id),
            {"file": open(file, "rb")},
            format="multipart",
        )
        self.assertTrue(status.is_success(response.status_code))

        file = testdata_path("delta/project.qgs")
        response = self.client.post(
            "/api/v1/files/{}/project.qgs/".format(self.project1.id),
            {"file": open(file, "rb")},
            format="multipart",
        )
        self.assertTrue(status.is_success(response.status_code))

        # Launch the export
        response = self.client.post(
            "/api/v1/qfield-files/export/{}/".format(self.project1.id),
        )
        self.assertTrue(status.is_success(response.status_code))

        # Wait for the worker to finish
        for _ in range(10):
            time.sleep(3)
            response = self.client.get(
                "/api/v1/qfield-files/export/{}/".format(self.project1.id),
            )
            payload = response.json()
            if payload["status"] == "STATUS_EXPORTED":
                response = self.client.get(
                    f"/api/v1/qfield-files/{self.project1.id}/project_qfield.qgs/"
                )

                self.assertIsInstance(response, HttpResponseRedirect)

                temp_dir = tempfile.mkdtemp()
                local_file = os.path.join(temp_dir, "project.qgs")

                # We cannot use the self.client HTTP client, since it does not support
                # requests outside the current Django App
                # Using the rest_api_framework.RequestsClient is not much better, so better
                # use the `requests` module

                with requests.get(response.url, stream=True) as r:
                    with open(local_file, "wb") as f:
                        for chunk in r.iter_content():
                            f.write(chunk)

                with open(local_file, "r") as f:
                    self.assertEqual(
                        f.readline().strip(),
                        "<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>",
                    )
                return
            elif payload["status"] == "STATUS_ERROR":
                self.fail("Worker failed with error")

        self.fail("Worker didn't finish")

    def test_list_files_for_qfield_broken_file(self):
        self.client.credentials(HTTP_AUTHORIZATION="Token " + self.token1.key)

        # Add files to the project
        file = testdata_path("delta/broken.qgs")
        response = self.client.post(
            "/api/v1/files/{}/broken.qgs/".format(self.project1.id),
            {"file": open(file, "rb")},
            format="multipart",
        )
        self.assertTrue(status.is_success(response.status_code))

        # Launch the export
        response = self.client.post(
            "/api/v1/qfield-files/export/{}/".format(self.project1.id),
        )
        self.assertTrue(status.is_success(response.status_code))

        # Wait for the worker to finish
        for _ in range(10):
            time.sleep(3)
            response = self.client.get(
                "/api/v1/qfield-files/export/{}/".format(self.project1.id),
            )
            if response.json()["status"] == "STATUS_ERROR":
                return

        self.fail("Worker didn't finish")

    def test_downloaded_file_has_canvas_name(self):
        self.client.credentials(HTTP_AUTHORIZATION="Token " + self.token1.key)

        # Add files to the project
        file = testdata_path("delta/points.geojson")
        response = self.client.post(
            "/api/v1/files/{}/points.geojson/".format(self.project1.id),
            {"file": open(file, "rb")},
            format="multipart",
        )
        self.assertTrue(status.is_success(response.status_code))

        file = testdata_path("delta/polygons.geojson")
        response = self.client.post(
            "/api/v1/files/{}/polygons.geojson/".format(self.project1.id),
            {"file": open(file, "rb")},
            format="multipart",
        )
        self.assertTrue(status.is_success(response.status_code))

        file = testdata_path("delta/project.qgs")
        response = self.client.post(
            "/api/v1/files/{}/project.qgs/".format(self.project1.id),
            {"file": open(file, "rb")},
            format="multipart",
        )
        self.assertTrue(status.is_success(response.status_code))

        # Launch the export
        response = self.client.post(
            "/api/v1/qfield-files/export/{}/".format(self.project1.id),
        )
        self.assertTrue(status.is_success(response.status_code))

        # Wait for the worker to finish
        for _ in range(10):
            time.sleep(3)
            response = self.client.get(
                "/api/v1/qfield-files/export/{}/".format(self.project1.id),
            )

            payload = response.json()
            if payload["status"] == "STATUS_EXPORTED":
                response = self.client.get(
                    f"/api/v1/qfield-files/{self.project1.id}/project_qfield.qgs/"
                )

                self.assertIsInstance(response, HttpResponseRedirect)

                temp_dir = tempfile.mkdtemp()
                local_file = os.path.join(temp_dir, "project.qgs")

                # We cannot use the self.client HTTP client, since it does not support
                # requests outside the current Django App
                # Using the rest_api_framework.RequestsClient is not much better, so better
                # use the `requests` module
                with requests.get(response.url, stream=True) as r:
                    with open(local_file, "wb") as f:
                        for chunk in r.iter_content():
                            f.write(chunk)

                with open(local_file, "r") as f:
                    for line in f:
                        if 'name="theMapCanvas"' in line:
                            return
            elif payload["status"] == "STATUS_ERROR":
                self.fail("Worker failed with error")

        self.fail("Worker didn't finish or there was an error")

    def test_download_project_with_broken_layer_datasources(self):
        self.client.credentials(HTTP_AUTHORIZATION="Token " + self.token1.key)

        # Add files to the project
        file = testdata_path("delta/points.geojson")
        response = self.client.post(
            "/api/v1/files/{}/points.geojson/".format(self.project1.id),
            {"file": open(file, "rb")},
            format="multipart",
        )
        self.assertTrue(status.is_success(response.status_code))

        file = testdata_path("delta/project_broken_datasource.qgs")
        response = self.client.post(
            "/api/v1/files/{}/project.qgs/".format(self.project1.id),
            {"file": open(file, "rb")},
            format="multipart",
        )
        self.assertTrue(status.is_success(response.status_code))

        # Launch the export
        response = self.client.post(
            "/api/v1/qfield-files/export/{}/".format(self.project1.id),
        )
        self.assertTrue(status.is_success(response.status_code))

        # Wait for the worker to finish
        for _ in range(10):
            time.sleep(3)
            response = self.client.get(
                "/api/v1/qfield-files/export/{}/".format(self.project1.id),
            )
            payload = response.json()
            if payload["status"] == "STATUS_EXPORTED":

                response = self.client.get(
                    "/api/v1/qfield-files/{}/".format(self.project1.id),
                )

                export_payload = response.json()
                layer_ok = export_payload["layers"][
                    "points_c2784cf9_c9c3_45f6_9ce5_98a6047e4d6c"
                ]
                layer_failed = export_payload["layers"][
                    "surfacestructure_35131bca_337c_483b_b09e_1cf77b1dfb16"
                ]

                self.assertTrue(layer_ok["valid"], layer_ok["status"])
                self.assertFalse(layer_failed["valid"], layer_failed["status"])
                return
            elif payload["status"] == "STATUS_ERROR":
                self.fail("Worker failed with error")

        self.fail("Worker didn't finish")

    def test_filename_with_whitespace(self):
        self.client.credentials(HTTP_AUTHORIZATION="Token " + self.token1.key)

        # Add files to the project
        file = testdata_path("delta/points.geojson")
        response = self.client.post(
            "/api/v1/files/{}/points.geojson/".format(self.project1.id),
            {"file": open(file, "rb")},
            format="multipart",
        )
        self.assertTrue(status.is_success(response.status_code))

        file = testdata_path("delta/polygons.geojson")
        response = self.client.post(
            "/api/v1/files/{}/polygons.geojson/".format(self.project1.id),
            {"file": open(file, "rb")},
            format="multipart",
        )
        self.assertTrue(status.is_success(response.status_code))

        file = testdata_path("delta/project.qgs")
        response = self.client.post(
            "/api/v1/files/{}/whitespace project.qgs/".format(self.project1.id),
            {"file": open(file, "rb")},
            format="multipart",
        )
        self.assertTrue(status.is_success(response.status_code))

        # Launch the export
        response = self.client.post(
            "/api/v1/qfield-files/export/{}/".format(self.project1.id),
        )
        self.assertTrue(status.is_success(response.status_code))

        # Wait for the worker to finish
        for _ in range(10):
            time.sleep(3)
            response = self.client.get(
                "/api/v1/qfield-files/export/{}/".format(self.project1.id),
            )

            payload = response.json()
            if payload["status"] == "STATUS_EXPORTED":
                return
            elif payload["status"] == "STATUS_ERROR":
                self.fail("Worker failed with error")

        self.fail("Worker didn't finish or there was an error")
